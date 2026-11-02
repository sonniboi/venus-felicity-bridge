"""Fakes for the Venus OS modules (vedbus, dbus, gi) so the bridges run without D-Bus."""
import json
import os
import sys
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "tests", "fixtures")
sys.path.insert(0, ROOT)


class FakeVeDbusService(object):
    instances = []

    def __init__(self, name, register=True, **kwargs):
        self.name = name
        self.values = {}
        self.writeable = {}
        self.registered = False
        self.register_calls = 0
        self.values_at_register = None
        FakeVeDbusService.instances.append(self)
        if register:
            self.register()

    def add_path(self, path, value, writeable=False, **kwargs):
        if path in self.values:
            raise ValueError("path added twice: %s" % path)
        self.values[path] = value
        self.writeable[path] = writeable

    def register(self):
        self.registered = True
        self.register_calls += 1
        self.values_at_register = dict(self.values)

    def __getitem__(self, path):
        return self.values[path]

    def __setitem__(self, path, value):
        if path not in self.values:
            raise KeyError("unknown D-Bus path: %s" % path)
        self.values[path] = value


def _install_fakes():
    vedbus = types.ModuleType("vedbus")
    vedbus.VeDbusService = FakeVeDbusService
    dbus = types.ModuleType("dbus")
    dbus_mainloop = types.ModuleType("dbus.mainloop")
    dbus_glib = types.ModuleType("dbus.mainloop.glib")
    dbus_glib.DBusGMainLoop = lambda set_as_default=False: None
    gi = types.ModuleType("gi")
    gi_repo = types.ModuleType("gi.repository")
    gi_repo.GLib = types.SimpleNamespace(timeout_add_seconds=lambda *a, **k: None, MainLoop=None)
    sys.modules.update({
        "vedbus": vedbus, "dbus": dbus, "dbus.mainloop": dbus_mainloop,
        "dbus.mainloop.glib": dbus_glib, "gi": gi, "gi.repository": gi_repo,
    })


_install_fakes()


@pytest.fixture
def fake_service():
    FakeVeDbusService.instances = []
    return FakeVeDbusService


def load_fixture(name):
    with open(os.path.join(FIXTURES, name)) as fh:
        return json.load(fh)


@pytest.fixture
def api():
    return {n: load_fixture("api_battery%d.json" % n) for n in (1, 2, 3)}


@pytest.fixture
def snapshot():
    return load_fixture("dbus_snapshot.json")


@pytest.fixture
def site_config():
    """Configuration matching the system the fixtures were captured on."""
    import felicity_config
    return felicity_config.load(os.path.join(FIXTURES, "site.ini"))
