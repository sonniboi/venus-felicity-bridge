"""Golden reference: with the captured API responses as input, the refactored code
must publish exactly the D-Bus paths of the original services and the same values.

The API responses and the D-Bus snapshot were captured a few seconds apart on a
live system (see fixtures/README.md). Pack 1 moved in between, so its fast
measurements (and the bank values derived from them) are compared with a
tolerance. Everything else must match exactly. "Invalid" (an empty variant in the
capture) equals None.
"""
import math

import pytest

import battery_dbus_bridge
import combined_battery_dbus

# Paths whose value depends on the moment of measurement
DRIFT = {
    "/Dc/0/Voltage": 0.05,
    "/Dc/0/Current": 3.0,
    "/Dc/0/Power": 160.0,
    "/System/MinCellVoltage": 0.005,
    "/System/MaxCellVoltage": 0.005,
    "/System/MaxVoltageCellId": None,  # any cell may be the highest at near-equal voltages
    "/System/MinVoltageCellId": None,
}


def _norm(v):
    return None if v == [] else v


def _assert_same(got, expected, drift_allowed):
    assert set(got) == set(expected), "path set differs: +%s -%s" % (
        sorted(set(got) - set(expected)), sorted(set(expected) - set(got)))
    for path, exp in expected.items():
        exp, val = _norm(exp), got[path]
        tol = None
        if drift_allowed:
            if path in DRIFT:
                tol = DRIFT[path]
            elif path.startswith("/Voltages/Cell"):
                tol = 0.005
        if drift_allowed and path in DRIFT and tol is None:
            continue
        if isinstance(exp, float) or isinstance(val, float):
            assert val is not None and exp is not None, path
            assert math.isclose(val, exp, rel_tol=1e-9, abs_tol=tol or 1e-9), (path, val, exp)
        else:
            assert val == exp, (path, val, exp)


@pytest.fixture
def modules(site_config):
    battery_dbus_bridge.import_venus_modules(site_config.velib_path)
    combined_battery_dbus.import_venus_modules(site_config.velib_path)


@pytest.mark.parametrize("pack", [1, 2, 3])
def test_pack_service_matches_snapshot(pack, api, snapshot, site_config, modules, fake_service):
    bridge = battery_dbus_bridge.FelicityDbusBridge(pack, site_config)
    bridge.create_dbus_service()
    assert bridge.update_dbus(api[pack]) is True
    _assert_same(bridge.dbus_service.values, snapshot["felicity_battery%d" % pack], drift_allowed=(pack == 1))


def test_bank_service_matches_snapshot(api, snapshot, site_config, modules, fake_service):
    bank = combined_battery_dbus.CombinedBank(site_config)
    bank.create_dbus_service()
    assert bank.poll_once([api[1], api[2], api[3]]) is True
    _assert_same(bank.dbus_service.values, snapshot["felicity_combined"], drift_allowed=True)


def test_bank_soc_exact(api, site_config, modules):
    # SOC, capacity and temperature do not drift within seconds -> exact
    bank = combined_battery_dbus.CombinedBank(site_config)
    data = bank.aggregate([api[1], api[2], api[3]])
    assert round(data["soc"], 1) == 68.5
    assert data["installed_capacity"] == 860
    assert data["pack_count"] == 3
