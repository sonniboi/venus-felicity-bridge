#!/usr/bin/env python3
"""
Felicity BMS D-Bus Bridge
Runs as separate process for each battery
Usage: battery_dbus_bridge.py <battery_num> [data_source_url]

v1.4 - Added Alarm/Fault Status forwarding to D-Bus/MQTT
v1.9 - SOH, MOS temp, sensors 5-8, full fault bits C01-C13
v2.0 - Added MaxVoltageCellId, MinVoltageCellId, MaxCellTemperature, MinCellTemperature,
       MaxTemperatureCellId, MinTemperatureCellId, InstalledCapacity
v2.1 - Debounce for transient fault alarms: alarm bits require ALARM_DEBOUNCE_POLLS
       consecutive active polls before being published to D-Bus (default 20 = 60s)
"""
import sys
import time
import logging
import json
import urllib.request

sys.path.insert(1, '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python')
from vedbus import VeDbusService
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

DBusGMainLoop(set_as_default=True)

class FelicityDbusBridge:
    def __init__(self, battery_num: int, data_source: str = "http://127.0.0.1:8080"):
        self.battery_num = battery_num
        self.data_source = data_source
        self.api_url = f"{data_source}/api/battery{battery_num}.json"
        self.dbus_service = None
        self.update_counter = 0
        self.error_counter = 0

        # Debounce: number of consecutive active polls required before an alarm
        # is promoted to D-Bus. At 3s poll interval, 20 polls = 60 seconds.
        self.ALARM_DEBOUNCE_POLLS = 20

        # Per-alarm-key consecutive-active-poll counters.
        # Keys match the alarm dict keys returned by parse_fault_status().
        self._alarm_debounce = {}

        # Per-alarm-key: current value that has been published to D-Bus.
        # Kept separate so we can clear D-Bus immediately when a fault clears.
        self._alarm_published = {}

        # Setup logging with battery_num context
        logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
        self.logger = logging.getLogger(f"Battery{battery_num}")
        
    def create_dbus_service(self):
        """Create D-Bus service for this battery"""
        service_name = f'com.victronenergy.battery.felicity_battery{self.battery_num}'
        self.dbus_service = VeDbusService(service_name, register=False)
        
        # Management paths
        self.dbus_service.add_path('/Mgmt/ProcessName', f'Felicity D-Bus Bridge #{self.battery_num}')
        self.dbus_service.add_path('/Mgmt/ProcessVersion', '2.0')
        self.dbus_service.add_path('/Mgmt/Connection', f'Serial /dev/ttyFelicityBMS (Modbus 0x{ {1:1,2:2,3:4}.get(self.battery_num, self.battery_num):02d})')
        
        # Product information
        self.dbus_service.add_path('/ProductName', f'Felicity BMS Battery {self.battery_num}')
        self.dbus_service.add_path('/ProductId', 0)
        self.dbus_service.add_path('/FirmwareVersion', '417')
        self.dbus_service.add_path('/HardwareVersion', '')
        
        # Serial numbers (different for each battery)
        _serials = {1: 'SERIAL-PACK-1', 2: 'SERIAL-PACK-2',
                    3: 'SERIAL-PACK-3'}  # 3 = FLA48460TG2-EU (Modbus-Client 4)
        self.dbus_service.add_path('/Serial', _serials.get(self.battery_num, f'felicity-{self.battery_num}'))
        
        self.dbus_service.add_path('/Connected', 1)
        self.dbus_service.add_path('/DeviceInstance', self.battery_num)
        self.dbus_service.add_path('/CustomName', f'Felicity Battery {self.battery_num}')
        
        # Battery measurements
        self.dbus_service.add_path('/Soc', None)
        self.dbus_service.add_path('/Dc/0/Voltage', None)
        self.dbus_service.add_path('/Dc/0/Current', None)
        self.dbus_service.add_path('/Dc/0/Power', None)
        self.dbus_service.add_path('/Dc/0/Temperature', None)
        
        # Cell voltages
        for i in range(1, 17):
            self.dbus_service.add_path(f'/Voltages/Cell{i}', None)
        
        # Temperature sensors
        for i in range(1, 5):
            self.dbus_service.add_path(f'/System/Temperature{i}', None)
        
        # System information
        self.dbus_service.add_path('/System/NrOfCellsPerBattery', 16)
        self.dbus_service.add_path('/System/MinCellVoltage', None)
        self.dbus_service.add_path('/System/MaxCellVoltage', None)
        self.dbus_service.add_path('/System/MaxVoltageCellId', None)
        self.dbus_service.add_path('/System/MinVoltageCellId', None)
        self.dbus_service.add_path('/System/MaxCellTemperature', None)
        self.dbus_service.add_path('/System/MinCellTemperature', None)
        self.dbus_service.add_path('/System/MaxTemperatureCellId', None)
        self.dbus_service.add_path('/System/MinTemperatureCellId', None)
        self.dbus_service.add_path('/InstalledCapacity', None)
        
        # History
        self.dbus_service.add_path('/History/ChargeCycles', None)
        # /History/ChargedEnergy 2026-08-01 entfernt (Register liefert keine Energie)
        # /History/DischargedEnergy 2026-08-01 entfernt (Register liefert keine Energie)
        self.dbus_service.add_path('/System/BMSUptime', None)
        self.dbus_service.add_path('/Soh', None)
        self.dbus_service.add_path('/System/MOSTemperature', None)
        for i in range(5, 9):
            self.dbus_service.add_path(f'/System/Temperature{i}', None)
        
        # Alarms (0=OK, 1=Warning, 2=Alarm)
        self.dbus_service.add_path('/Alarms/LowVoltage', 0)
        self.dbus_service.add_path('/Alarms/HighVoltage', 0)
        self.dbus_service.add_path('/Alarms/LowSoc', 0)
        self.dbus_service.add_path('/Alarms/HighTemperature', 0)
        self.dbus_service.add_path('/Alarms/LowTemperature', 0)
        self.dbus_service.add_path('/Alarms/HighChargeCurrent', 0)
        self.dbus_service.add_path('/Alarms/HighDischargeCurrent', 0)
        self.dbus_service.add_path('/Alarms/Alarm', 0)  # General alarm indicator for GUI
        
        # Extended Fault Info (custom paths for detailed fault status)
        self.dbus_service.add_path('/Alarms/FaultStatusText', 'None')
        self.dbus_service.add_path('/Alarms/FaultRaw', 0)
        self.dbus_service.add_path('/Alarms/CellImbalance', 0)
        self.dbus_service.add_path('/Alarms/SensorFault', 0)
        self.dbus_service.add_path('/Alarms/ParallelFault', 0)
        self.dbus_service.add_path('/Alarms/OutputResistanceLow', 0)
        self.dbus_service.add_path('/Alarms/MOSTemperatureLow', 0)
        self.dbus_service.add_path('/Alarms/ChargeEnabled', 1)
        self.dbus_service.add_path('/Alarms/DischargeEnabled', 1)

        # Register the service
        self.dbus_service.register()
        
        self.logger.info(f"D-Bus service created: com.victronenergy.battery.felicity_battery{self.battery_num}")
    
    def fetch_battery_data(self):
        """Fetch battery data from web interface API"""
        try:
            with urllib.request.urlopen(self.api_url, timeout=5) as response:
                data = json.loads(response.read().decode('utf-8'))
                return data
        except Exception as e:
            self.error_counter += 1
            if self.error_counter % 10 == 1:  # Log every 10th error to avoid spam
                self.logger.warning(f"[Battery {self.battery_num}] Error fetching data from {self.api_url}: {e} (error #{self.error_counter})")
            return None
    
    def parse_fault_status(self, fault_text):
        """Parse fault status text and return individual alarm states"""
        alarms = {
            'high_voltage': 0,
            'low_voltage': 0,
            'high_charge_current': 0,
            'high_discharge_current': 0,
            'high_temperature': 0,
            'low_temperature': 0,
            'general_alarm': 0
        }
        
        if not fault_text or fault_text == "None":
            return alarms
        
        # Parse the fault text
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
    
    def update_dbus(self):
        """Update D-Bus values from battery data"""
        data = self.fetch_battery_data()
        if not data:
            self.dbus_service['/Connected'] = 0  # Mark as disconnected if no data
            return False

        try:
            # Update SOC
            # Nur plausible Werte publizieren: 0 (oder None) heisst hier "noch kein
            # Modbus-Read durch" bzw. Lesefehler — wuerde in HA als SOC-Einbruch auf
            # 0 % landen. Im Betrieb schaltet das BMS lange vor 0 % ab.
            soc = data.get("SOC", {}).get("value")
            if soc is not None and 0 < soc <= 100:
                self.dbus_service['/Soc'] = soc
                # Low SOC alarm (below 10%)
                if soc < 10:
                    self.dbus_service['/Alarms/LowSoc'] = 2
                elif soc < 20:
                    self.dbus_service['/Alarms/LowSoc'] = 1
                else:
                    self.dbus_service['/Alarms/LowSoc'] = 0

            # Update voltage
            if "BatteryVoltage" in data:
                voltage = data["BatteryVoltage"]["value"]
                self.dbus_service['/Dc/0/Voltage'] = voltage

            # Update current and power
            if "BatteryCurrent" in data:
                current = data["BatteryCurrent"]["value"]
                # 2026-08-06: Victron-Konvention (positiv = laden). Die fruehere
                # Inversion war ein EVCC-Workaround, den jetzt der HA-Adapter
                # sensor.evcc_batterieleistung uebernimmt.
                self.dbus_service['/Dc/0/Current'] = current
                if "BatteryVoltage" in data:
                    self.dbus_service['/Dc/0/Power'] = voltage * current

            # Update cell voltages and find min/max
            cell_voltages = []
            for i in range(1, 17):
                key = f"Cell{i} Voltage"
                if key in data:
                    cell_v = data[key]["value"]
                    self.dbus_service[f'/Voltages/Cell{i}'] = cell_v
                    cell_voltages.append(cell_v)

            # Update min/max cell voltage + cell IDs
            if cell_voltages:
                self.dbus_service['/System/MinCellVoltage'] = min(cell_voltages)
                self.dbus_service['/System/MaxCellVoltage'] = max(cell_voltages)
            if "MaxCellVoltageId" in data:
                self.dbus_service['/System/MaxVoltageCellId'] = data["MaxCellVoltageId"]["value"]
            if "MinCellVoltageId" in data:
                self.dbus_service['/System/MinVoltageCellId'] = data["MinCellVoltageId"]["value"]

            # Update temperature sensors
            for i in range(1, 5):
                key = f"Sensor{i} Temperature"
                if key in data:
                    self.dbus_service[f'/System/Temperature{i}'] = data[key]["value"]

            # Update max/min cell temperature + cell IDs
            if "MaxCellTemperature" in data:
                self.dbus_service['/System/MaxCellTemperature'] = data["MaxCellTemperature"]["value"]
            if "MinCellTemperature" in data:
                self.dbus_service['/System/MinCellTemperature'] = data["MinCellTemperature"]["value"]
            if "MaxCellTemperatureId" in data:
                self.dbus_service['/System/MaxTemperatureCellId'] = data["MaxCellTemperatureId"]["value"]
            if "MinCellTemperatureId" in data:
                self.dbus_service['/System/MinTemperatureCellId'] = data["MinCellTemperatureId"]["value"]

            # Update installed capacity (static per battery)
            if "RatedCapacity" in data:
                self.dbus_service['/InstalledCapacity'] = data["RatedCapacity"]["value"]

            # Update SOH
            if "SOH" in data:
                self.dbus_service['/Soh'] = data["SOH"]["value"]

            # Update MOS temperature
            if "MOSTemperature" in data:
                self.dbus_service['/System/MOSTemperature'] = data["MOSTemperature"]["value"]

            # Update temperature sensors 5-8
            for i in range(5, 9):
                key = f"Sensor{i} Temperature"
                if key in data:
                    self.dbus_service[f'/System/Temperature{i}'] = data[key]["value"]

            # Update raw fault register
            if "FaultRaw" in data:
                self.dbus_service['/Alarms/FaultRaw'] = data["FaultRaw"]["value"]

            # Update cycle count
            if "CycleCount" in data:
                self.dbus_service['/History/ChargeCycles'] = data["CycleCount"]["value"]

            # Update energy counters (kWh)
            if "ChgEnergy" in data:
                self.dbus_service['/History/ChargedEnergy'] = data["ChgEnergy"]["value"]
            if "DcgEnergy" in data:
                self.dbus_service['/History/DischargedEnergy'] = data["DcgEnergy"]["value"]

            # Update BMS uptime (hours)
            if "BMSUptime" in data:
                self.dbus_service['/System/BMSUptime'] = data["BMSUptime"]["value"]

            # Update firmware version
            if "BMSVersion" in data:
                self.dbus_service['/FirmwareVersion'] = str(data["BMSVersion"]["value"])

            # ===== ALARM/FAULT STATUS UPDATE (with debounce) =====
            if "FaultStatus" in data:
                fault_text = data["FaultStatus"]["value"]
                self.dbus_service['/Alarms/FaultStatusText'] = fault_text

                # Parse raw alarm state for this poll
                alarms = self.parse_fault_status(fault_text)

                # Map from alarm dict key -> D-Bus path
                alarm_dbus_map = {
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

                debounced_general_alarm = 0

                for key, dbus_path in alarm_dbus_map.items():
                    raw_val = alarms.get(key, 0)

                    if raw_val > 0:
                        # Fault active this poll: increment debounce counter
                        self._alarm_debounce[key] = self._alarm_debounce.get(key, 0) + 1
                        count = self._alarm_debounce[key]

                        if count == 1:
                            # First occurrence — log but do NOT publish yet
                            self.logger.warning(
                                f"[Battery {self.battery_num}] Transient fault detected "
                                f"{key}={raw_val} (poll 1/{self.ALARM_DEBOUNCE_POLLS}, "
                                f"FaultStatus={fault_text}) — debouncing, not yet on D-Bus"
                            )
                        elif count == self.ALARM_DEBOUNCE_POLLS:
                            # Threshold reached — promote to D-Bus alarm
                            self.dbus_service[dbus_path] = raw_val
                            self._alarm_published[key] = raw_val
                            self.logger.warning(
                                f"[Battery {self.battery_num}] ALARM confirmed after "
                                f"{self.ALARM_DEBOUNCE_POLLS} polls: {key}={raw_val}, "
                                f"FaultStatus={fault_text}"
                            )
                        elif count > self.ALARM_DEBOUNCE_POLLS:
                            # Already published, keep value current (no-op if unchanged)
                            pass

                        # Contribute to general alarm only if debounce threshold is met
                        if count >= self.ALARM_DEBOUNCE_POLLS:
                            debounced_general_alarm = 2

                    else:
                        # Fault clear this poll
                        prev_count = self._alarm_debounce.get(key, 0)
                        if prev_count > 0 and prev_count < self.ALARM_DEBOUNCE_POLLS:
                            # Was in debounce window — fault resolved before threshold
                            self.logger.warning(
                                f"[Battery {self.battery_num}] Transient fault cleared "
                                f"{key} after {prev_count} polls "
                                f"(< {self.ALARM_DEBOUNCE_POLLS} threshold) — "
                                f"was NOT published to D-Bus"
                            )
                        # Reset counter regardless
                        self._alarm_debounce[key] = 0

                        # Clear D-Bus only if we had previously published an alarm
                        if self._alarm_published.get(key, 0) > 0:
                            self.dbus_service[dbus_path] = 0
                            self._alarm_published[key] = 0
                            self.logger.info(
                                f"[Battery {self.battery_num}] Alarm cleared on D-Bus: {key}"
                            )

                # Update general alarm indicator (debounced aggregate)
                self.dbus_service['/Alarms/Alarm'] = debounced_general_alarm
            
            # Update Charge/Discharge Enable status
            if "ChargeEnabled" in data:
                self.dbus_service['/Alarms/ChargeEnabled'] = 1 if data["ChargeEnabled"]["value"] == "Yes" else 0
            if "DischargeEnabled" in data:
                self.dbus_service['/Alarms/DischargeEnabled'] = 1 if data["DischargeEnabled"]["value"] == "Yes" else 0

            # Mark as connected
            self.dbus_service['/Connected'] = 1

            # Log every 20 updates (60 seconds at 3s interval)
            self.update_counter += 1
            if self.update_counter % 20 == 1:
                soc = data.get("SOC", {}).get("value", "N/A")
                voltage = data.get("BatteryVoltage", {}).get("value", "N/A")
                fault = data.get("FaultStatus", {}).get("value", "N/A")
                self.logger.info(f"[Battery {self.battery_num}] Update #{self.update_counter}: SoC={soc}%, V={voltage}V, Fault={fault}")

            # Reset error counter on successful update
            if self.error_counter > 0:
                self.logger.info(f"[Battery {self.battery_num}] Connection restored after {self.error_counter} errors")
                self.error_counter = 0

            return True

        except Exception as e:
            self.logger.error(f"[Battery {self.battery_num}] Error updating D-Bus values: {e}")
            self.dbus_service['/Connected'] = 0
            return False
    
    def wait_for_api(self, max_wait=120):
        """Wait until the battery API is available before registering on D-Bus.
        Prevents crash-loop when battery_web_interface.py hasn't started yet."""
        self.logger.info(f"[Battery {self.battery_num}] Waiting for API at {self.api_url}...")
        deadline = time.time() + max_wait
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(self.api_url, timeout=3) as r:
                    if r.status == 200:
                        self.logger.info(f"[Battery {self.battery_num}] API ready.")
                        return True
            except Exception:
                pass
            time.sleep(3)
        self.logger.error(f"[Battery {self.battery_num}] API not available after {max_wait}s, continuing anyway.")
        return False

    def run(self):
        """Main run loop with restart-on-error.
        Wraps GLib mainloop in a retry loop so daemontools doesn't give up
        after rapid crashes (e.g. D-Bus disconnect, temporary API unavailability)."""
        RETRY_DELAY = 10  # seconds between restart attempts

        # Wait for API before first D-Bus registration
        self.wait_for_api()

        while True:
            try:
                self.dbus_service = None  # Reset for clean re-registration
                self.create_dbus_service()

                def update_callback():
                    self.update_dbus()
                    return True  # Continue calling

                GLib.timeout_add_seconds(3, update_callback)

                self.logger.info("Starting GLib mainloop...")
                mainloop = GLib.MainLoop()
                mainloop.run()

                # mainloop.run() returned cleanly — unusual but not fatal
                self.logger.warning("GLib mainloop exited cleanly, restarting...")

            except Exception as e:
                self.logger.error(f"[Battery {self.battery_num}] Main loop error: {e} — restarting in {RETRY_DELAY}s")

            time.sleep(RETRY_DELAY)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: battery_dbus_bridge.py <battery_num> [data_source_url]")
        sys.exit(1)
    
    battery_num = int(sys.argv[1])
    data_source = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8080"
    
    bridge = FelicityDbusBridge(battery_num, data_source)
    bridge.run()
