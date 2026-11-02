# venus-felicity-bridge

Initial import (2026-11-02) of two Venus OS D-Bus services that publish Felicity Solar
LiFePO4 battery packs as `com.victronenergy.battery.*` services:

- `battery_dbus_bridge.py` — one D-Bus battery service per pack
- `combined_battery_dbus.py` — aggregated bank service

Both read pack data from a local HTTP JSON API (not included). This import is
unmodified apart from redacted serial numbers; configuration layer, documentation,
tests and license follow. Register background: `sonniboi/felicity-bms-notes`.
