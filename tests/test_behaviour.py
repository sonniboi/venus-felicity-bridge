import copy

import pytest

import battery_dbus_bridge
import combined_battery_dbus
import felicity_config


@pytest.fixture(autouse=True)
def modules(site_config):
    battery_dbus_bridge.import_venus_modules(site_config.velib_path)
    combined_battery_dbus.import_venus_modules(site_config.velib_path)


# --- registration: never with made-up zeros ---------------------------------

def test_pack_service_registers_without_zero_measurements(site_config, fake_service):
    bridge = battery_dbus_bridge.FelicityDbusBridge(1, site_config)
    bridge.create_dbus_service()
    s = bridge.dbus_service
    assert s.register_calls == 1
    for path in ("/Soc", "/Dc/0/Voltage", "/Dc/0/Current", "/Dc/0/Power", "/Voltages/Cell1"):
        assert s.values_at_register[path] is None, path


def test_bank_does_not_register_before_valid_data(site_config, fake_service):
    clock = [0.0]
    bank = combined_battery_dbus.CombinedBank(site_config, clock=lambda: clock[0])
    bank.create_dbus_service()
    bank.fetch_all = lambda: [None, None, None]

    def sleep(sec):
        assert not bank.dbus_service.registered  # still hidden while waiting
        clock[0] += sec

    assert bank.register_when_ready(sleep=sleep) is False
    s = bank.dbus_service
    assert s.registered
    assert s.values_at_register["/Soc"] is None  # timeout: "invalid", never 0


def test_bank_registers_with_real_values(api, site_config, fake_service):
    bank = combined_battery_dbus.CombinedBank(site_config)
    bank.create_dbus_service()
    bank.fetch_all = lambda: [api[1], api[2], api[3]]
    assert bank.register_when_ready(sleep=lambda s: None) is True
    assert bank.dbus_service.values_at_register["/Soc"] == 68.5


# --- pack bridge ---------------------------------------------------------------

def test_soc_zero_is_not_published(api, site_config, fake_service):
    bridge = battery_dbus_bridge.FelicityDbusBridge(1, site_config)
    bridge.create_dbus_service()
    data = copy.deepcopy(api[1])
    data["SOC"]["value"] = 0
    bridge.update_dbus(data)
    assert bridge.dbus_service["/Soc"] is None


def test_no_data_marks_disconnected(site_config, fake_service):
    bridge = battery_dbus_bridge.FelicityDbusBridge(1, site_config)
    bridge.create_dbus_service()
    bridge.fetch_battery_data = lambda: None
    assert bridge.update_dbus() is False
    assert bridge.dbus_service["/Connected"] == 0


def test_energy_counters_in_api_do_not_break_the_update(api, site_config, fake_service):
    # The original script wrote ChgEnergy/DcgEnergy to paths that no longer exist,
    # which raised and marked the pack disconnected.
    bridge = battery_dbus_bridge.FelicityDbusBridge(1, site_config)
    bridge.create_dbus_service()
    data = copy.deepcopy(api[1])
    data["ChgEnergy"] = {"value": 1.0, "unit": "kWh"}
    data["DcgEnergy"] = {"value": 2.0, "unit": "kWh"}
    assert bridge.update_dbus(data) is True
    assert bridge.dbus_service["/Connected"] == 1


def test_fault_is_debounced_and_cleared_at_once(api, site_config, fake_service):
    bridge = battery_dbus_bridge.FelicityDbusBridge(1, site_config)
    bridge.create_dbus_service()
    s = bridge.dbus_service
    data = copy.deepcopy(api[1])
    data["FaultStatus"]["value"] = "C13"
    for _ in range(site_config.alarm_debounce_polls - 1):
        bridge.update_dbus(data)
    assert s["/Alarms/ParallelFault"] == 0 and s["/Alarms/Alarm"] == 0
    bridge.update_dbus(data)
    assert s["/Alarms/ParallelFault"] == 2 and s["/Alarms/Alarm"] == 2
    data["FaultStatus"]["value"] = "None"
    bridge.update_dbus(data)
    assert s["/Alarms/ParallelFault"] == 0 and s["/Alarms/Alarm"] == 0


def test_parse_fault_status():
    alarms = battery_dbus_bridge.parse_fault_status("Cell V High, C11")
    assert alarms["high_voltage"] == 2 and alarms["sensor_fault"] == 2 and alarms["general_alarm"] == 2
    assert battery_dbus_bridge.parse_fault_status("None")["general_alarm"] == 0


# --- bank aggregation ----------------------------------------------------------

def test_soc_is_capacity_weighted(api, site_config):
    bank = combined_battery_dbus.CombinedBank(site_config)
    data = bank.aggregate([api[1], api[2], api[3]])
    caps = [api[n]["RatedCapacity"]["value"] for n in (1, 2, 3)]
    socs = [api[n]["SOC"]["value"] for n in (1, 2, 3)]
    assert data["soc"] == pytest.approx(sum(c * s for c, s in zip(caps, socs)) / sum(caps))
    assert data["current"] == pytest.approx(sum(api[n]["BatteryCurrent"]["value"] for n in (1, 2, 3)))


def test_pack_without_live_data_is_carried_forward_then_dropped(api, site_config):
    clock = [1000.0]
    bank = combined_battery_dbus.CombinedBank(site_config, clock=lambda: clock[0])
    full = bank.aggregate([api[1], api[2], api[3]])
    static_only = {"RatedCapacity": api[3]["RatedCapacity"], "SOC": {"value": 0, "unit": "%"}}

    clock[0] += 60
    carried = bank.aggregate([api[1], api[2], static_only])
    assert carried["soc"] == pytest.approx(full["soc"])          # no jump
    assert carried["installed_capacity"] == full["installed_capacity"]
    assert carried["pack_count"] == 2                             # live values from 2 packs

    clock[0] += site_config.pack_soc_ttl + 1
    dropped = bank.aggregate([api[1], api[2], static_only])
    assert dropped["installed_capacity"] == full["installed_capacity"] - api[3]["RatedCapacity"]["value"]


def test_bank_needs_two_live_packs(api, site_config):
    bank = combined_battery_dbus.CombinedBank(site_config)
    assert bank.aggregate([api[1], None, None]) is None


def test_bank_requires_configured_packs(api, site_config, fake_service):
    bank = combined_battery_dbus.CombinedBank(site_config)
    bank.create_dbus_service()
    bank.registered = True
    assert bank.poll_once([api[1], None, api[3]]) is False   # pack 2 is required
    assert bank.dbus_service["/Connected"] == 0
    assert bank.poll_once([api[1], api[2], None]) is True    # pack 3 is optional


def test_implausible_bank_soc_is_not_published(site_config, fake_service):
    bank = combined_battery_dbus.CombinedBank(site_config)
    bank.create_dbus_service()
    assert bank.publish({"soc": 0}) is False
    assert bank.dbus_service["/Soc"] is None


# --- configuration -------------------------------------------------------------

def test_defaults_without_file(tmp_path, monkeypatch):
    monkeypatch.delenv("FELICITY_BRIDGE_CONFIG", raising=False)
    monkeypatch.setattr(felicity_config, "DEFAULT_PATHS", [str(tmp_path / "missing.ini")])
    cfg = felicity_config.load()
    assert cfg.api_base_url == "http://127.0.0.1:8080"
    assert cfg.pack_numbers == [1, 2, 3] and cfg.required_packs == [1, 2]
    assert cfg.bank_device_instance == 512
    assert cfg.pack(2).serial == "felicity-2"


def test_explicit_missing_file_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        felicity_config.load(str(tmp_path / "nope.ini"))


def test_config_argument_parsing():
    assert felicity_config.config_path_from_argv(["1", "--config", "a.ini"]) == ("a.ini", ["1"])
    assert felicity_config.config_path_from_argv(["--config=b.ini", "2", "http://x"]) == ("b.ini", ["2", "http://x"])


def test_example_config_loads():
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.example.ini")
    cfg = felicity_config.load(path)
    assert cfg.pack_numbers and cfg.bank_device_instance
