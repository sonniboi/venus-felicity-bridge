#!/usr/bin/env python3
"""
Felicity BMS D-Bus bridge - one process per battery pack.

Reads the pack values from the local pack API (/api/battery<N>.json, see
docs/api-contract.md) and publishes them as a Venus OS battery service
com.victronenergy.battery.felicity_battery<N>.

Usage: battery_dbus_bridge.py <pack_number> [api_base_url] [--config <file>]

History of the original script:
  v1.4  alarm/fault status forwarded to D-Bus
  v1.9  SOH, MOS temperature, sensors 5-8, fault bits C01-C13
  v2.0  min/max cell voltage and temperature with cell ids, installed capacity
  v2.1  debounce for transient fault alarms
  v2.2  configuration file, English comments, testable structure (no functional change)
"""
import sys
import time
import logging
import json
import urllib.request

import felicity_config

VeDbusService = None
GLib = None


def import_venus_modules(velib_path):
    """Import the Venus OS D-Bus helpers. Done at runtime so tests can inject fakes."""
    global VeDbusService, GLib
    if velib_path not in sys.path:
        sys.path.insert(1, velib_path)
    from vedbus import VeDbusService as _VeDbusService
    from dbus.mainloop.glib import DBusGMainLoop
    from gi.repository import GLib as _GLib
    DBusGMainLoop(set_as_default=True)
    VeDbusService, GLib = _VeDbusService, _GLib


# Fault flags reported by the pack -> D-Bus alarm path
ALARM_DBUS_MAP = {
    'high_voltage':           '/Alarms/HighVoltage',
    'low_voltage':            '/Alarms/LowVoltage',
    'high_charge_current':    '/Alarms/HighChargeCurrent',
    'high_discharge_current': '/Alarms/HighDischargeCurrent',
    'high_temperature':       '/Alarms/HighTemperature',
    'low_temperature':        '/Alarms/LowTemperature',
    'sensor_fault':           '/Alarms/SensorFault',
    'parallel_fault':         '/Alarms/ParallelFault',
    'output_res_low':         '/Alarms/OutputResistanceLow',
    'mos_temp_low':           '/Alarms/MOSTemperatureLow',
}


def parse_fault_status(fault_text):
    """Translate the pack's fault text into individual alarm states (0 = ok, 2 = alarm)."""
    alarms = {
        'high_voltage': 0,
        'low_voltage': 0,
        'high_charge_current': 0,
        'high_discharge_current': 0,
        'high_temperature': 0,
        'low_temperature': 0,
        'general_alarm': 0,
    }
    if not fault_text or fault_text == "None":
        return alarms

    fault_lower = fault_text.lower()
    if "cell v high" in fault_lower:
        alarms['high_voltage'] = 2
        alarms['general_alarm'] = 2
    if "cell v low" in fault_lower:
        alarms['low_voltage'] = 2
        alarms['general_alarm'] = 2
    if "charge i high" in fault_lower:
        alarms['high_charge_current'] = 2
        alarms['general_alarm'] = 2
    if "discharge i high" in fault_lower:
        alarms['high_discharge_current'] = 2
        alarms['general_alarm'] = 2
    if "mos temp high" in fault_lower or "cell temp high" in fault_lower:
        alarms['high_temperature'] = 2
        alarms['general_alarm'] = 2
    if "mos temp low" in fault_lower or "cell temp low" in fault_lower:
        alarms['low_temperature'] = 2
        alarms['general_alarm'] = 2
    if "c01" in fault_lower:
        alarms['high_voltage'] = 2
        alarms['general_alarm'] = 2
    if "c02" in fault_lower:
        alarms['low_voltage'] = 2
        alarms['general_alarm'] = 2
    alarms['sensor_fault'] = 2 if "c11" in fault_lower else 0
    alarms['parallel_fault'] = 2 if "c13" in fault_lower else 0
    alarms['output_res_low'] = 2 if "c12" in fault_lower else 0
    alarms['mos_temp_low'] = 2 if "c08" in fault_lower else 0
    return alarms


class FelicityDbusBridge(object):
    def __init__(self, pack_number, config=None, api_base_url=None):
        self.config = config or felicity_config.BridgeConfig()
        self.pack = self.config.pack(pack_number)
        self.battery_num = pack_number
        base = (api_base_url or self.config.api_base_url).rstrip("/")
        self.api_url = "%s/api/battery%d.json" % (base, pack_number)
        self.dbus_service = None
        self.update_counter = 0
        self.error_counter = 0

        self.ALARM_DEBOUNCE_POLLS = self.config.alarm_debounce_polls
        # Consecutive polls each fault has been active
        self._alarm_debounce = {}
        # Value currently published per fault, so a cleared fault is removed at once
        self._alarm_published = {}

        logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
        self.logger = logging.getLogger("Battery%d" % pack_number)

    def create_dbus_service(self):
        """Create the D-Bus service for this pack.

        The service is created with register=False and all measurements start as
        None (D-Bus "invalid"). It is registered only after all paths exist and the
        pack API has answered (see run()), so consumers never see a made-up 0.
        """
        p = self.pack
        service_name = 'com.victronenergy.battery.felicity_battery%d' % self.battery_num
        self.dbus_service = VeDbusService(service_name, register=False)
        s = self.dbus_service

        s.add_path('/Mgmt/ProcessName', 'Felicity D-Bus Bridge #%d' % self.battery_num)
        s.add_path('/Mgmt/ProcessVersion', '2.0')
        s.add_path('/Mgmt/Connection', 'Serial %s (Modbus 0x%02d)' % (p.serial_port, p.modbus_address))

        s.add_path('/ProductName', p.product_name)
        s.add_path('/ProductId', 0)
        s.add_path('/FirmwareVersion', p.firmware_version)
        s.add_path('/HardwareVersion', '')
        s.add_path('/Serial', p.serial)
        s.add_path('/Connected', 1)
        s.add_path('/DeviceInstance', p.device_instance)
        s.add_path('/CustomName', p.custom_name)

        s.add_path('/Soc', None)
        s.add_path('/Dc/0/Voltage', None)
        s.add_path('/Dc/0/Current', None)
        s.add_path('/Dc/0/Power', None)
        s.add_path('/Dc/0/Temperature', None)

        for i in range(1, self.config.cells_per_pack + 1):
            s.add_path('/Voltages/Cell%d' % i, None)
        for i in range(1, 5):
            s.add_path('/System/Temperature%d' % i, None)

        s.add_path('/System/NrOfCellsPerBattery', self.config.cells_per_pack)
        s.add_path('/System/MinCellVoltage', None)
        s.add_path('/System/MaxCellVoltage', None)
        s.add_path('/System/MaxVoltageCellId', None)
        s.add_path('/System/MinVoltageCellId', None)
        s.add_path('/System/MaxCellTemperature', None)
        s.add_path('/System/MinCellTemperature', None)
        s.add_path('/System/MaxTemperatureCellId', None)
        s.add_path('/System/MinTemperatureCellId', None)
        s.add_path('/InstalledCapacity', None)

        s.add_path('/History/ChargeCycles', None)
        # No /History/ChargedEnergy and /History/DischargedEnergy: the BMS registers
        # behind them do not hold usable energy counters, so they are not published.
        s.add_path('/System/BMSUptime', None)
        s.add_path('/Soh', None)
        s.add_path('/System/MOSTemperature', None)
        for i in range(5, 9):
            s.add_path('/System/Temperature%d' % i, None)

        # Alarms: 0 = ok, 1 = warning, 2 = alarm
        s.add_path('/Alarms/LowVoltage', 0)
        s.add_path('/Alarms/HighVoltage', 0)
        s.add_path('/Alarms/LowSoc', 0)
        s.add_path('/Alarms/HighTemperature', 0)
        s.add_path('/Alarms/LowTemperature', 0)
        s.add_path('/Alarms/HighChargeCurrent', 0)
        s.add_path('/Alarms/HighDischargeCurrent', 0)
        s.add_path('/Alarms/Alarm', 0)  # general alarm indicator for the GUI

        # Extended fault information (custom paths)
        s.add_path('/Alarms/FaultStatusText', 'None')
        s.add_path('/Alarms/FaultRaw', 0)
        s.add_path('/Alarms/CellImbalance', 0)
        s.add_path('/Alarms/SensorFault', 0)
        s.add_path('/Alarms/ParallelFault', 0)
        s.add_path('/Alarms/OutputResistanceLow', 0)
        s.add_path('/Alarms/MOSTemperatureLow', 0)
        s.add_path('/Alarms/ChargeEnabled', 1)
        s.add_path('/Alarms/DischargeEnabled', 1)

        s.register()
        self.logger.info("D-Bus service created: %s", service_name)

    def fetch_battery_data(self):
        """Fetch the pack values from the local pack API."""
        try:
            with urllib.request.urlopen(self.api_url, timeout=self.config.api_timeout) as response:
                return json.loads(response.read().decode('utf-8'))
        except Exception as e:
            self.error_counter += 1
            if self.error_counter % 10 == 1:  # log every 10th error only
                self.logger.warning("[Battery %d] Error fetching data from %s: %s (error #%d)",
                                    self.battery_num, self.api_url, e, self.error_counter)
            return None

    # kept as a method for compatibility with the original class interface
    def parse_fault_status(self, fault_text):
        return parse_fault_status(fault_text)

    def update_dbus(self, data=None):
        """Copy one API response onto D-Bus. Returns True on success."""
        if data is None:
            data = self.fetch_battery_data()
        s = self.dbus_service
        if not data:
            s['/Connected'] = 0
            return False

        try:
            # SOC: only plausible values. 0 (or None) means "no Modbus read yet" or a
            # read error - publishing it would show up as a SOC drop to 0 %. In normal
            # operation the BMS disconnects long before 0 %.
            soc = data.get("SOC", {}).get("value")
            if soc is not None and 0 < soc <= 100:
                s['/Soc'] = soc
                if soc < 10:
                    s['/Alarms/LowSoc'] = 2
                elif soc < 20:
                    s['/Alarms/LowSoc'] = 1
                else:
                    s['/Alarms/LowSoc'] = 0

            if "BatteryVoltage" in data:
                voltage = data["BatteryVoltage"]["value"]
                s['/Dc/0/Voltage'] = voltage

            if "BatteryCurrent" in data:
                current = data["BatteryCurrent"]["value"]
                # Victron convention: positive = charging (the API already delivers it so)
                s['/Dc/0/Current'] = current
                if "BatteryVoltage" in data:
                    s['/Dc/0/Power'] = voltage * current

            cell_voltages = []
            for i in range(1, self.config.cells_per_pack + 1):
                key = "Cell%d Voltage" % i
                if key in data:
                    cell_v = data[key]["value"]
                    s['/Voltages/Cell%d' % i] = cell_v
                    cell_voltages.append(cell_v)

            if cell_voltages:
                s['/System/MinCellVoltage'] = min(cell_voltages)
                s['/System/MaxCellVoltage'] = max(cell_voltages)
            if "MaxCellVoltageId" in data:
                s['/System/MaxVoltageCellId'] = data["MaxCellVoltageId"]["value"]
            if "MinCellVoltageId" in data:
                s['/System/MinVoltageCellId'] = data["MinCellVoltageId"]["value"]

            for i in range(1, 5):
                key = "Sensor%d Temperature" % i
                if key in data:
                    s['/System/Temperature%d' % i] = data[key]["value"]

            if "MaxCellTemperature" in data:
                s['/System/MaxCellTemperature'] = data["MaxCellTemperature"]["value"]
            if "MinCellTemperature" in data:
                s['/System/MinCellTemperature'] = data["MinCellTemperature"]["value"]
            if "MaxCellTemperatureId" in data:
                s['/System/MaxTemperatureCellId'] = data["MaxCellTemperatureId"]["value"]
            if "MinCellTemperatureId" in data:
                s['/System/MinTemperatureCellId'] = data["MinCellTemperatureId"]["value"]

            if "RatedCapacity" in data:
                s['/InstalledCapacity'] = data["RatedCapacity"]["value"]
            if "SOH" in data:
                s['/Soh'] = data["SOH"]["value"]
            if "MOSTemperature" in data:
                s['/System/MOSTemperature'] = data["MOSTemperature"]["value"]

            for i in range(5, 9):
                key = "Sensor%d Temperature" % i
                if key in data:
                    s['/System/Temperature%d' % i] = data[key]["value"]

            if "FaultRaw" in data:
                s['/Alarms/FaultRaw'] = data["FaultRaw"]["value"]
            if "CycleCount" in data:
                s['/History/ChargeCycles'] = data["CycleCount"]["value"]
            # Energy counters are intentionally not published (see create_dbus_service).
            if "BMSUptime" in data:
                s['/System/BMSUptime'] = data["BMSUptime"]["value"]
            if "BMSVersion" in data:
                s['/FirmwareVersion'] = str(data["BMSVersion"]["value"])

            if "FaultStatus" in data:
                self._update_alarms(data["FaultStatus"]["value"])

            if "ChargeEnabled" in data:
                s['/Alarms/ChargeEnabled'] = 1 if data["ChargeEnabled"]["value"] == "Yes" else 0
            if "DischargeEnabled" in data:
                s['/Alarms/DischargeEnabled'] = 1 if data["DischargeEnabled"]["value"] == "Yes" else 0

            s['/Connected'] = 1

            self.update_counter += 1
            if self.update_counter % 20 == 1:
                self.logger.info("[Battery %d] Update #%d: SoC=%s%%, V=%sV, Fault=%s",
                                 self.battery_num, self.update_counter,
                                 data.get("SOC", {}).get("value", "N/A"),
                                 data.get("BatteryVoltage", {}).get("value", "N/A"),
                                 data.get("FaultStatus", {}).get("value", "N/A"))

            if self.error_counter > 0:
                self.logger.info("[Battery %d] Connection restored after %d errors",
                                 self.battery_num, self.error_counter)
                self.error_counter = 0
            return True

        except Exception as e:
            self.logger.error("[Battery %d] Error updating D-Bus values: %s", self.battery_num, e)
            s['/Connected'] = 0
            return False

    def _update_alarms(self, fault_text):
        """Publish fault alarms with debounce.

        Packs report short fault blips (for example while the parallel bus
        re-negotiates). A fault is raised on D-Bus only after it was present in
        ALARM_DEBOUNCE_POLLS consecutive polls; it is cleared immediately.
        """
        s = self.dbus_service
        s['/Alarms/FaultStatusText'] = fault_text
        alarms = parse_fault_status(fault_text)
        debounced_general_alarm = 0

        for key, dbus_path in ALARM_DBUS_MAP.items():
            raw_val = alarms.get(key, 0)
            if raw_val > 0:
                self._alarm_debounce[key] = self._alarm_debounce.get(key, 0) + 1
                count = self._alarm_debounce[key]
                if count == 1:
                    self.logger.warning(
                        "[Battery %d] Transient fault detected %s=%s (poll 1/%d, FaultStatus=%s) "
                        "- debouncing, not yet on D-Bus",
                        self.battery_num, key, raw_val, self.ALARM_DEBOUNCE_POLLS, fault_text)
                elif count == self.ALARM_DEBOUNCE_POLLS:
                    s[dbus_path] = raw_val
                    self._alarm_published[key] = raw_val
                    self.logger.warning(
                        "[Battery %d] ALARM confirmed after %d polls: %s=%s, FaultStatus=%s",
                        self.battery_num, self.ALARM_DEBOUNCE_POLLS, key, raw_val, fault_text)
                if count >= self.ALARM_DEBOUNCE_POLLS:
                    debounced_general_alarm = 2
            else:
                prev_count = self._alarm_debounce.get(key, 0)
                if 0 < prev_count < self.ALARM_DEBOUNCE_POLLS:
                    self.logger.warning(
                        "[Battery %d] Transient fault cleared %s after %d polls (< %d threshold) "
                        "- was NOT published to D-Bus",
                        self.battery_num, key, prev_count, self.ALARM_DEBOUNCE_POLLS)
                self._alarm_debounce[key] = 0
                if self._alarm_published.get(key, 0) > 0:
                    s[dbus_path] = 0
                    self._alarm_published[key] = 0
                    self.logger.info("[Battery %d] Alarm cleared on D-Bus: %s", self.battery_num, key)

        s['/Alarms/Alarm'] = debounced_general_alarm

    def wait_for_api(self, max_wait=120):
        """Wait until the pack API answers before registering on D-Bus.
        Avoids a crash loop while the API service is still starting."""
        self.logger.info("[Battery %d] Waiting for API at %s...", self.battery_num, self.api_url)
        deadline = time.time() + max_wait
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(self.api_url, timeout=3) as r:
                    if r.status == 200:
                        self.logger.info("[Battery %d] API ready.", self.battery_num)
                        return True
            except Exception:
                pass
            time.sleep(3)
        self.logger.error("[Battery %d] API not available after %ds, continuing anyway.",
                          self.battery_num, max_wait)
        return False

    def run(self):
        """Main loop with restart-on-error, so daemontools does not give up after
        rapid crashes (D-Bus disconnect, API temporarily unavailable)."""
        retry_delay = 10
        self.wait_for_api()
        while True:
            try:
                self.dbus_service = None
                self.create_dbus_service()

                def update_callback():
                    self.update_dbus()
                    return True

                GLib.timeout_add_seconds(self.config.poll_interval, update_callback)
                self.logger.info("Starting GLib mainloop...")
                GLib.MainLoop().run()
                self.logger.warning("GLib mainloop exited cleanly, restarting...")
            except Exception as e:
                self.logger.error("[Battery %d] Main loop error: %s - restarting in %ds",
                                  self.battery_num, e, retry_delay)
            time.sleep(retry_delay)


def main(argv):
    config_path, args = felicity_config.config_path_from_argv(argv[1:])
    if not args:
        print("Usage: battery_dbus_bridge.py <pack_number> [api_base_url] [--config <file>]")
        return 1
    config = felicity_config.load(config_path)
    import_venus_modules(config.velib_path)
    api_base_url = args[1] if len(args) > 1 else None
    FelicityDbusBridge(int(args[0]), config, api_base_url).run()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
