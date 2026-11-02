#!/usr/bin/env python3
"""
Combined Felicity battery bank - aggregates all packs into one virtual battery
(com.victronenergy.battery.felicity_combined) for the Venus OS GUI, DVCC and
everything that reads the system battery.

Usage: combined_battery_dbus.py [--config <file>]

Aggregation rules (all packs sit in parallel on the same DC bus):
  SOC          capacity-weighted mean (an arithmetic mean under-weights a big pack)
  voltage      mean of the packs
  current      sum, Victron convention (positive = charging)
  temperature  mean of all sensors of all packs
  cells        numbered consecutively: pack 1 = 1..16, pack 2 = 17..32, ...
"""
import sys
import time
import logging
import json
import urllib.request

import felicity_config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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


def fetch_battery_data(url, timeout=5):
    """Fetch one pack from the local pack API."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except Exception as e:
        logger.error("Error fetching %s: %s", url, e)
        return None


class CombinedBank(object):
    def __init__(self, config=None, clock=time.time):
        self.config = config or felicity_config.BridgeConfig()
        self.clock = clock
        self.dbus_service = None
        self.registered = False
        # Last valid SOC per pack position: position -> (timestamp, soc, capacity).
        # If a pack drops out briefly (bridge restart, Modbus timeout) the capacity
        # weighting would change abruptly - with one 460 Ah pack next to two 200 Ah
        # packs the bank SOC jumped by more than 15 points. The SOC of a LiFePO4 pack
        # hardly moves within minutes, so the last value is carried forward for
        # pack_soc_ttl seconds instead of dropping the pack. After that it leaves the
        # weighting (one explained jump is safer than a permanently frozen SOC).
        self._pack_soc_cache = {}

    @property
    def cell_count(self):
        return self.config.cells_per_pack * len(self.config.pack_numbers)

    def aggregate(self, pack_data):
        """Aggregate a list of pack API responses (None = pack not available).

        Returns None if fewer than two packs deliver live data.
        """
        cfg = self.config

        def val(p, key, default=0):
            v = p.get(key, {}).get("value", default)
            return default if v is None else v

        # Only packs WITH live data take part. A pack with static fields only
        # (serial number, capacity, but no SOC) would enter the weighting with
        # SOC 0 and distort the bank SOC (observed: 28.8 % instead of 62 %).
        # SOC 0 counts as invalid: a freshly started pack service reports 0 before
        # its first Modbus read; in operation the BMS disconnects long before 0 %.
        now = self.clock()
        packs = []       # packs with live data (voltage, current, cells, temperatures)
        soc_terms = []   # (capacity, soc) for the weighting, incl. carried-forward packs
        for idx, p in enumerate(pack_data):
            raw_soc = p.get("SOC", {}).get("value") if p else None
            cap = (val(p, "RatedCapacity", cfg.default_capacity) or cfg.default_capacity) if p \
                else cfg.default_capacity
            if raw_soc is not None and 0 < raw_soc <= 100:
                packs.append(p)
                soc_terms.append((cap, raw_soc))
                self._pack_soc_cache[idx] = (now, raw_soc, cap)
                continue
            cached = self._pack_soc_cache.get(idx)
            if cached and now - cached[0] <= cfg.pack_soc_ttl:
                soc_terms.append((cached[2], cached[1]))
                logger.warning("Pack %d without valid SOC - carrying forward last value %.1f%% (%.0fs old)",
                               idx + 1, cached[1], now - cached[0])
            elif cached:
                logger.warning("Pack %d without SOC for >%ds - removed from the weighting",
                               idx + 1, cfg.pack_soc_ttl)
                del self._pack_soc_cache[idx]

        if len(packs) < 2:
            return None

        total_cap = sum(c for c, _ in soc_terms)
        soc = sum(c * s for c, s in soc_terms) / total_cap if total_cap else 0

        voltages = [val(p, "BatteryVoltage") for p in packs]
        voltage = sum(voltages) / len(voltages) if voltages else 0

        current = sum(val(p, "BatteryCurrent") for p in packs)
        power = voltage * current

        temps = []
        for p in packs:
            for i in range(1, 5):
                t = p.get("Sensor%d Temperature" % i, {}).get("value", None)
                if t is not None:
                    temps.append(t)
        temperature = sum(temps) / len(temps) if temps else 20.0

        cycles = max(val(p, "CycleCount") for p in packs)

        n = cfg.cells_per_pack
        cells = {}
        for idx, p in enumerate(packs):
            for i in range(1, n + 1):
                cells["Cell%d" % (idx * n + i)] = p.get("Cell%d Voltage" % i, {}).get("value", 0)

        all_cell_voltages = [v for v in cells.values() if v and v > 0]
        min_cell_v = min(all_cell_voltages) if all_cell_voltages else 0
        max_cell_v = max(all_cell_voltages) if all_cell_voltages else 0
        values = list(cells.values())
        min_cell_id = values.index(min_cell_v) + 1 if min_cell_v else 0
        max_cell_id = values.index(max_cell_v) + 1 if max_cell_v else 0

        return {
            "soc": soc,
            "voltage": voltage,
            "current": current,
            "power": power,
            "temperature": temperature,
            "cycles": cycles,
            "cells": cells,
            "min_cell_voltage": min_cell_v,
            "max_cell_voltage": max_cell_v,
            "min_cell_id": min_cell_id,
            "max_cell_id": max_cell_id,
            "max_cell_temperature": max(temps) if temps else temperature,
            "min_cell_temperature": min(temps) if temps else temperature,
            "installed_capacity": total_cap,
            "pack_count": len(packs),
        }

    def publish(self, data):
        """Write aggregated values to D-Bus. Never publishes an implausible SOC."""
        s = self.dbus_service
        if s is None or not data:
            return False
        if not (0 < data['soc'] <= 100):
            logger.warning("Implausible bank SOC %.1f%% - not published", data['soc'])
            return False
        try:
            s['/Soc'] = round(data['soc'], 1)
            s['/Dc/0/Voltage'] = round(data['voltage'], 2)
            s['/Dc/0/Current'] = round(data['current'], 2)
            s['/Dc/0/Power'] = round(data['power'], 2)
            s['/Dc/0/Temperature'] = round(data['temperature'], 1)
            s['/History/ChargeCycles'] = int(data['cycles'])

            # State: 0 = idle, 1 = charging, 2 = discharging
            if data['current'] > 0.5:
                s['/State'] = 1
            elif data['current'] < -0.5:
                s['/State'] = 2
            else:
                s['/State'] = 0

            for i in range(1, self.cell_count + 1):
                key = "Cell%d" % i
                if key in data['cells']:
                    s['/Voltages/Cell%d' % i] = round(data['cells'][key], 3)

            s['/System/MinCellVoltage'] = round(data['min_cell_voltage'], 3)
            s['/System/MaxCellVoltage'] = round(data['max_cell_voltage'], 3)
            s['/System/MinVoltageCellId'] = data['min_cell_id']
            s['/System/MaxVoltageCellId'] = data['max_cell_id']
            s['/System/MaxCellTemperature'] = round(data['max_cell_temperature'], 1)
            s['/System/MinCellTemperature'] = round(data['min_cell_temperature'], 1)
            s['/InstalledCapacity'] = data['installed_capacity']
            s['/Connected'] = 1
            return True
        except Exception as e:
            logger.error("Error publishing to D-Bus: %s", e)
            return False

    def fetch_all(self):
        cfg = self.config
        return [fetch_battery_data(cfg.api_url(n), cfg.api_timeout) for n in cfg.pack_numbers]

    def poll_once(self, pack_data=None):
        """One fetch/publish cycle. True if valid values are on D-Bus."""
        try:
            if pack_data is None:
                pack_data = self.fetch_all()
            by_number = dict(zip(self.config.pack_numbers, pack_data))
            if all(by_number.get(n) for n in self.config.required_packs):
                combined = self.aggregate(pack_data)
                if combined and self.publish(combined):
                    logger.info("Combined battery (%d packs, %.0fAh): %.1f%% SOC, %.2fV, %.2fA, %.1f°C",
                                combined['pack_count'], combined['installed_capacity'], combined['soc'],
                                combined['voltage'], combined['current'], combined['temperature'])
                    return True
            else:
                logger.warning("Could not fetch the required packs %s", self.config.required_packs)
                if self.registered:
                    self.dbus_service['/Connected'] = 0
        except Exception as e:
            logger.error("Error in poll_once: %s", e)
        return False

    def create_dbus_service(self):
        """Create the bank service.

        register=False: the service must not appear on D-Bus before real values are
        in it. Otherwise dbus-mqtt (and everything behind it, e.g. Home Assistant)
        receives the initial values and every restart shows up as a SOC drop to 0 %.
        """
        cfg = self.config
        s = VeDbusService('com.victronenergy.battery.felicity_combined', register=False)
        self.dbus_service = s

        s.add_path('/DeviceInstance', cfg.bank_device_instance)
        s.add_path('/ProductId', 0)
        s.add_path('/ProductName', cfg.bank_product_name)
        s.add_path('/FirmwareVersion', cfg.bank_firmware_version)
        s.add_path('/HardwareVersion', '')
        s.add_path('/Serial', cfg.bank_serial)
        s.add_path('/CustomName', cfg.bank_custom_name, writeable=True)
        s.add_path('/Connected', 1)

        s.add_path('/Mgmt/ProcessName', 'Combined Felicity Battery Manager')
        s.add_path('/Mgmt/ProcessVersion', '2.3')
        s.add_path('/Mgmt/Connection', cfg.bank_connection)

        # Measurements start as None (D-Bus "invalid"), not 0: if the service ever has
        # to register without valid data (see register_timeout), consumers show
        # "unknown" instead of a real-looking 0.
        s.add_path('/Soc', None, writeable=False)
        s.add_path('/Dc/0/Voltage', None, writeable=False)
        s.add_path('/Dc/0/Current', None, writeable=False)
        s.add_path('/Dc/0/Power', None, writeable=False)
        s.add_path('/Dc/0/Temperature', None, writeable=False)
        s.add_path('/State', 0, writeable=False)
        s.add_path('/History/ChargeCycles', 0, writeable=False)

        s.add_path('/System/NrOfCellsPerBattery', self.cell_count)
        s.add_path('/System/MinCellVoltage', None, writeable=False)
        s.add_path('/System/MaxCellVoltage', None, writeable=False)
        s.add_path('/System/MinVoltageCellId', 0, writeable=False)
        s.add_path('/System/MaxVoltageCellId', 0, writeable=False)
        s.add_path('/System/MaxCellTemperature', 0, writeable=False)
        s.add_path('/System/MinCellTemperature', 0, writeable=False)
        s.add_path('/InstalledCapacity', 0, writeable=False)
        s.add_path('/System/Temperature1', 0, writeable=False)

        for i in range(1, self.cell_count + 1):
            s.add_path('/Voltages/Cell%d' % i, 0, writeable=False)

        s.add_path('/Alarms/LowVoltage', 0, writeable=False)
        s.add_path('/Alarms/HighVoltage', 0, writeable=False)
        s.add_path('/Alarms/LowSoc', 0, writeable=False)
        s.add_path('/Alarms/HighTemperature', 0, writeable=False)
        s.add_path('/Alarms/LowTemperature', 0, writeable=False)
        s.add_path('/Alarms/Alarm', 0, writeable=False)
        logger.info("Combined battery D-Bus service created: com.victronenergy.battery.felicity_combined")

    def register_when_ready(self, sleep=time.sleep):
        """Register only after the first valid values are published.

        The timeout is a safety net: if the service stayed unregistered forever
        (e.g. two packs dead at the same time), consumers would see the bank as
        unavailable indefinitely - and an external watchdog that power-cycles the
        GX device on "unavailable" would loop. After register_timeout the service
        appears with None values ("unknown"), but never with 0.
        """
        deadline = self.clock() + self.config.register_timeout
        attempt = 0
        got_data = False
        while self.clock() < deadline:
            if self.poll_once():
                got_data = True
                break
            attempt += 1
            if attempt % 6 == 1:
                logger.warning("Waiting for valid battery data (attempt %d), D-Bus service not registered yet...",
                               attempt)
            sleep(5)

        self.dbus_service.register()
        self.registered = True
        if got_data:
            logger.info("D-Bus service registered - first values already present")
        else:
            logger.error("D-Bus service registered after %ds without valid data (values stay 'invalid', not 0)",
                         self.config.register_timeout)
        return got_data


def main(argv):
    config_path, _ = felicity_config.config_path_from_argv(argv[1:])
    config = felicity_config.load(config_path)
    import_venus_modules(config.velib_path)
    logger.info("Starting Combined Felicity Battery Manager...")
    bank = CombinedBank(config)
    bank.create_dbus_service()
    bank.register_when_ready()

    def update_loop():
        bank.poll_once()
        return True

    GLib.timeout_add_seconds(config.bank_poll_interval, update_loop)
    logger.info("Combined Battery Manager running...")
    GLib.MainLoop().run()
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
