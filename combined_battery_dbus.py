#!/usr/bin/env python3
"""
Combined Felicity Battery - Aggregiert beide Batterien zu einer virtuellen Batterie
Zeigt aggregierte Werte im Venus OS GUI an
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

# Logging Setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DBusGMainLoop(set_as_default=True)

API_URL_1 = "http://localhost:8080/api/battery1.json"
API_URL_2 = "http://localhost:8080/api/battery2.json"
API_URL_3 = "http://localhost:8080/api/battery3.json"  # FLA48460TG2-EU (Modbus-Client 4)

dbus_service = None

# Letzter bekannter SOC je Pack, gegen SOC-Spruenge in HA (2026-08-01):
# Faellt ein Pack kurz aus (Bridge-Neustart, Modbus-Timeout), aendert sich sonst
# schlagartig die Kapazitaetsgewichtung — bei Pack 3 (460 von 860 Ah) sprang der
# Bank-SOC dadurch um >15 Prozentpunkte. Der SOC eines LiFePO4-Packs bewegt sich
# in Minuten kaum, deshalb wird er fuer PACK_SOC_TTL fortgeschrieben statt den
# Pack aus der Gewichtung zu werfen. Danach faellt er raus (ein begruendeter,
# einmaliger Sprung — ein dauerhaft eingefrorener SOC waere gefaehrlicher).
PACK_SOC_TTL = 900  # s
_pack_soc_cache = {}  # pack-index -> (timestamp, soc, capacity)
_registered = False   # True, sobald der Service auf dem D-Bus sichtbar ist

def fetch_battery_data(url):
    """Holt Batterie-Daten vom HTTP API"""
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return json.loads(response.read().decode())
    except Exception as e:
        logger.error(f"Fehler beim Abrufen von {url}: {e}")
        return None

def aggregate_batteries(bat1, bat2, bat3=None):
    """Aggregiert die parallelen Packs zu einer virtuellen Batterie.

    bat3 (FLA48460TG2-EU, 460 Ah, Modbus-Client 4) ist optional: faellt sie aus,
    laeuft die Aggregation mit den verbleibenden Packs weiter.
    SOC wird KAPAZITAETSGEWICHTET gerechnet (200/200/460) - ein arithmetisches
    Mittel wuerde den grossen Pack massiv unterrepraesentieren.
    """
    def val(p, key, default=0):
        v = p.get(key, {}).get("value", default)
        return default if v is None else v

    # Nur Packs MIT Live-Daten: ein Pack mit bloss statischen Feldern (SerialNumber/
    # Kapazitaet, aber kein SOC) wuerde mit SOC 0 in die Gewichtung eingehen und den
    # Gesamt-SOC verfaelschen (beobachtet 2026-07-25: 28,8 % statt 62 %).
    # SOC 0 gilt dabei als ungueltig: ein frisch gestarteter Pack-Service meldet 0,
    # bevor der erste Modbus-Read durch ist — im laufenden Betrieb schaltet das BMS
    # lange vor 0 % ab.
    now = time.time()
    packs = []          # Packs mit Live-Daten (Spannung, Strom, Zellen, Temperatur)
    soc_terms = []      # (Kapazitaet, SOC) fuer die Gewichtung, inkl. fortgeschriebener Packs
    for idx, p in enumerate((bat1, bat2, bat3)):
        raw_soc = p.get("SOC", {}).get("value") if p else None
        cap = (val(p, "RatedCapacity", 200) or 200) if p else 200
        if raw_soc is not None and 0 < raw_soc <= 100:
            packs.append(p)
            soc_terms.append((cap, raw_soc))
            _pack_soc_cache[idx] = (now, raw_soc, cap)
            continue
        cached = _pack_soc_cache.get(idx)
        if cached and now - cached[0] <= PACK_SOC_TTL:
            soc_terms.append((cached[2], cached[1]))
            logger.warning(f"Pack {idx + 1} ohne gueltigen SOC — schreibe letzten Wert "
                           f"{cached[1]:.1f}% ({now - cached[0]:.0f}s alt) fort")
        elif cached:
            logger.warning(f"Pack {idx + 1} seit >{PACK_SOC_TTL}s ohne SOC — faellt aus der Gewichtung")
            del _pack_soc_cache[idx]

    if len(packs) < 2:
        return None

    # SOC: kapazitaetsgewichtet statt arithmetisch
    # (Kapazitaeten stammen aus RatedCapacity/BATTERY_INFO ueber die API)
    total_cap = sum(c for c, _ in soc_terms)
    soc = sum(c * s for c, s in soc_terms) / total_cap if total_cap else 0

    # Spannung: Mittel (alle Packs parallel am selben DC-Bus)
    voltages = [val(p, "BatteryVoltage") for p in packs]
    voltage = sum(voltages) / len(voltages) if voltages else 0

    # Strom: Summe in Victron-Konvention (positiv = laden), seit 2026-08-06.
    # Die fruehere Negation war ein EVCC-Workaround (s. evcc_batterieleistung).
    current = sum(val(p, "BatteryCurrent") for p in packs)
    power = voltage * current

    # Temperatur: alle Sensoren aller Packs (4 pro Pack)
    temps = []
    for p in packs:
        for i in range(1, 5):
            t = p.get(f"Sensor{i} Temperature", {}).get("value", None)
            if t is not None:
                temps.append(t)
    temperature = sum(temps) / len(temps) if temps else 20.0

    cycles = max(val(p, "CycleCount") for p in packs)
    # charged_energy 2026-08-01 entfernt — Summe sinnloser Pack-Werte
    # discharged_energy 2026-08-01 entfernt — Summe sinnloser Pack-Werte

    # Zellspannungen fortlaufend: Pack1 1-16, Pack2 17-32, Pack3 33-48
    cells = {}
    for idx, p in enumerate(packs):
        for i in range(1, 17):
            cells[f"Cell{idx * 16 + i}"] = p.get(f"Cell{i} Voltage", {}).get("value", 0)

    all_cell_voltages = [v for v in cells.values() if v and v > 0]
    min_cell_v = min(all_cell_voltages) if all_cell_voltages else 0
    max_cell_v = max(all_cell_voltages) if all_cell_voltages else 0
    values = list(cells.values())
    min_cell_id = values.index(min_cell_v) + 1 if min_cell_v else 0
    max_cell_id = values.index(max_cell_v) + 1 if max_cell_v else 0

    max_cell_temp = max(temps) if temps else temperature
    min_cell_temp = min(temps) if temps else temperature

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
        "max_cell_temperature": max_cell_temp,
        "min_cell_temperature": min_cell_temp,
        "installed_capacity": total_cap,
        "pack_count": len(packs),
    }

def publish_to_dbus(data):
    """Published aggregierte Daten nach D-Bus"""
    if dbus_service is None or not data:
        return False

    if not (0 < data['soc'] <= 100):
        logger.warning(f"Unplausibler Bank-SOC {data['soc']:.1f}% — nicht publiziert")
        return False

    try:
        dbus_service['/Soc'] = round(data['soc'], 1)
        dbus_service['/Dc/0/Voltage'] = round(data['voltage'], 2)
        dbus_service['/Dc/0/Current'] = round(data['current'], 2)
        dbus_service['/Dc/0/Power'] = round(data['power'], 2)
        dbus_service['/Dc/0/Temperature'] = round(data['temperature'], 1)
        dbus_service['/History/ChargeCycles'] = int(data['cycles'])
        # ChargedEnergy 2026-08-01 entfernt
        # DischargedEnergy 2026-08-01 entfernt

        # State: 0=Idle, 1=Charging, 2=Discharging
        if data['current'] > 0.5:
            dbus_service['/State'] = 1  # Charging (Victron: positiv=laden)
        elif data['current'] < -0.5:
            dbus_service['/State'] = 2  # Discharging (Victron: negativ=entladen)
        else:
            dbus_service['/State'] = 0  # Idle

        # Zellspannungen (alle 32 Zellen)
        for i in range(1, 49):
            cell_key = f"Cell{i}"
            if cell_key in data['cells']:
                dbus_service[f'/Voltages/Cell{i}'] = round(data['cells'][cell_key], 3)

        # Min/Max Zellspannung + Cell IDs
        dbus_service['/System/MinCellVoltage'] = round(data['min_cell_voltage'], 3)
        dbus_service['/System/MaxCellVoltage'] = round(data['max_cell_voltage'], 3)
        dbus_service['/System/MinVoltageCellId'] = data['min_cell_id']
        dbus_service['/System/MaxVoltageCellId'] = data['max_cell_id']

        # Max/Min Zelltemperatur
        dbus_service['/System/MaxCellTemperature'] = round(data['max_cell_temperature'], 1)
        dbus_service['/System/MinCellTemperature'] = round(data['min_cell_temperature'], 1)

        # Installierte Kapazität
        dbus_service['/InstalledCapacity'] = data['installed_capacity']

        dbus_service['/Connected'] = 1
        return True
    except Exception as e:
        logger.error(f"Fehler beim Publizieren nach D-Bus: {e}")
        return False

def poll_once():
    """Ein Abruf-/Publish-Zyklus. True, wenn gueltige Werte auf dem D-Bus stehen."""
    try:
        # Hole Daten von beiden Batterien
        bat1 = fetch_battery_data(API_URL_1)
        bat2 = fetch_battery_data(API_URL_2)
        bat3 = fetch_battery_data(API_URL_3)  # optional, Ausfall degradiert nur

        if bat1 and bat2:
            # Aggregiere Batterien (bat3 optional)
            combined = aggregate_batteries(bat1, bat2, bat3)

            # Publiziere nach D-Bus
            if combined and publish_to_dbus(combined):
                logger.info(f"Combined Battery ({combined['pack_count']} Packs, {combined['installed_capacity']:.0f}Ah): {combined['soc']:.1f}% SOC, {combined['voltage']:.2f}V, {combined['current']:.2f}A, {combined['temperature']:.1f}°C")
                return True
        else:
            logger.warning("Konnte Batterie-Daten nicht abrufen")
            if _registered:
                dbus_service['/Connected'] = 0

    except Exception as e:
        logger.error(f"Fehler in poll_once: {e}")

    return False

def update_loop():
    """GLib-Timer-Callback"""
    poll_once()
    return True  # Continue timer

def create_dbus_service():
    """Erstellt den D-Bus Service für die kombinierte Batterie"""
    global dbus_service

    # register=False: Der Service darf erst auf dem D-Bus erscheinen, wenn echte
    # Werte drinstehen. Sonst publiziert dbus-mqtt die Initialwerte (/Soc = 0) nach
    # HA und jeder Service-Neustart erzeugt dort einen SOC-Einbruch auf 0 %
    # (nachgewiesen 2026-07-25 bis 2026-08-01). Gleiches Muster wie in
    # battery_dbus_bridge.py.
    dbus_service = VeDbusService('com.victronenergy.battery.felicity_combined', register=False)

    # Device Info
    dbus_service.add_path('/DeviceInstance', 512)  # Höhere Nummer als Battery 1/2
    dbus_service.add_path('/ProductId', 0)
    dbus_service.add_path('/ProductName', 'Felicity Battery Bank (3x16S)')
    dbus_service.add_path('/FirmwareVersion', '417')
    dbus_service.add_path('/HardwareVersion', '')
    dbus_service.add_path('/Serial', 'COMBINED_BANK')
    dbus_service.add_path('/CustomName', 'Felicity Battery Bank', writeable=True)
    dbus_service.add_path('/Connected', 1)

    # Management
    dbus_service.add_path('/Mgmt/ProcessName', 'Combined Felicity Battery Manager')
    dbus_service.add_path('/Mgmt/ProcessVersion', '2.3')
    dbus_service.add_path('/Mgmt/Connection', 'Aggregated from 2x Felicity BMS')

    # Battery Values
    # Messwerte starten als None (= D-Bus "invalid"), nicht als 0: sollte der Service
    # ausnahmsweise ohne gueltige Daten registrieren muessen (siehe REGISTER_TIMEOUT in
    # main), erscheint in HA dann "unknown" statt eines echt aussehenden 0-Werts.
    dbus_service.add_path('/Soc', None, writeable=False)
    dbus_service.add_path('/Dc/0/Voltage', None, writeable=False)
    dbus_service.add_path('/Dc/0/Current', None, writeable=False)
    dbus_service.add_path('/Dc/0/Power', None, writeable=False)
    dbus_service.add_path('/Dc/0/Temperature', None, writeable=False)
    dbus_service.add_path('/State', 0, writeable=False)  # Battery state: 0=Idle, 1=Charging, 2=Discharging
    dbus_service.add_path('/History/ChargeCycles', 0, writeable=False)
    # /History/ChargedEnergy 2026-08-01 entfernt
    # /History/DischargedEnergy 2026-08-01 entfernt

    # System Info
    dbus_service.add_path('/System/NrOfCellsPerBattery', 48)  # 3x16 Zellen (2x LPBF + FLA48460)
    dbus_service.add_path('/System/MinCellVoltage', None, writeable=False)
    dbus_service.add_path('/System/MaxCellVoltage', None, writeable=False)
    dbus_service.add_path('/System/MinVoltageCellId', 0, writeable=False)
    dbus_service.add_path('/System/MaxVoltageCellId', 0, writeable=False)
    dbus_service.add_path('/System/MaxCellTemperature', 0, writeable=False)
    dbus_service.add_path('/System/MinCellTemperature', 0, writeable=False)
    dbus_service.add_path('/InstalledCapacity', 0, writeable=False)
    dbus_service.add_path('/System/Temperature1', 0, writeable=False)

    # Alle 32 Zellspannungen (16 pro Batterie)
    for i in range(1, 49):
        dbus_service.add_path(f'/Voltages/Cell{i}', 0, writeable=False)

    # Alarms
    dbus_service.add_path('/Alarms/LowVoltage', 0, writeable=False)
    dbus_service.add_path('/Alarms/HighVoltage', 0, writeable=False)
    dbus_service.add_path('/Alarms/LowSoc', 0, writeable=False)
    dbus_service.add_path('/Alarms/HighTemperature', 0, writeable=False)
    dbus_service.add_path('/Alarms/LowTemperature', 0, writeable=False)
    dbus_service.add_path('/Alarms/Alarm', 0, writeable=False)  # General alarm indicator for GUI

    logger.info("Combined Battery D-Bus Service erstellt: com.victronenergy.battery.felicity_combined")

def main():
    global _registered

    logger.info("Starting Combined Felicity Battery Manager...")

    # Erstelle D-Bus Service (noch nicht registriert)
    create_dbus_service()

    # Erst echte Werte holen, DANN auf dem D-Bus erscheinen — verhindert den
    # 0-%-Spike in HA bei jedem Neustart des Services oder von Venus OS.
    # Der Timeout ist die Notbremse: bliebe der Service dauerhaft unregistriert
    # (z. B. zwei Packs gleichzeitig tot), sieht HA den Bank-SOC endlos als
    # unavailable — und die Automation "Venus OS Auto-Restart via USB-Switch"
    # (Trigger: unavailable fuer 2 min) wuerde den Pi in einer Schleife
    # power-cyclen. Nach REGISTER_TIMEOUT erscheint der Service deshalb mit
    # None-Werten ("unknown" in HA), aber niemals mit einer 0.
    REGISTER_TIMEOUT = 90  # s
    deadline = time.time() + REGISTER_TIMEOUT
    attempt = 0
    got_data = False
    while time.time() < deadline:
        if poll_once():
            got_data = True
            break
        attempt += 1
        if attempt % 6 == 1:  # ~alle 30 s eine Meldung, nicht bei jedem Versuch
            logger.warning(f"Warte auf gueltige Batteriedaten (Versuch {attempt}), "
                           f"D-Bus-Service noch nicht registriert...")
        time.sleep(5)

    dbus_service.register()
    _registered = True
    if got_data:
        logger.info("D-Bus Service registriert — erste Werte lagen bereits an")
    else:
        logger.error(f"D-Bus Service nach {REGISTER_TIMEOUT}s ohne gueltige Daten "
                     f"registriert (Werte bleiben 'invalid' statt 0)")

    # Start Update Loop (alle 5 Sekunden)
    GLib.timeout_add_seconds(5, update_loop)

    # Run MainLoop
    logger.info("Combined Battery Manager läuft...")
    mainloop = GLib.MainLoop()
    mainloop.run()

if __name__ == '__main__':
    main()
