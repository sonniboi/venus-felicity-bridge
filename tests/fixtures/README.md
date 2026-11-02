# Test fixtures (captured 2026-11-02, live system, night, no PV)

- `api_battery{1,2,3}.json` — responses of the local pack API (`/api/battery<n>.json`),
  every field `{"value": …, "unit": …}`. Packs 1–2: LPBF48200-P (16S), pack 3: FLA48460 (16S).
- `dbus_snapshot.json` — all D-Bus paths/values of the four running services
  (`com.victronenergy.battery.felicity_battery1..3`, `…felicity_combined`), captured
  at the same moment via `GetItems`. Use as golden reference: the refactored code must
  produce the same paths and (for these inputs) the same values.

Serial numbers are replaced by placeholders. Captures are a few seconds apart, so
small differences between API and D-Bus values (current, power) are expected.
