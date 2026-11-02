# venus-felicity-bridge

Venus OS D-Bus battery services for **Felicity Solar LiFePO4 packs** (tested with
LPBF48200-P and FLA48460, 16S, several packs in parallel on one DC bus).

- `battery_dbus_bridge.py` publishes one `com.victronenergy.battery.felicity_battery<N>`
  service per pack: SOC, voltage, current, power, 16 cell voltages, temperatures,
  SOH, cycles, BMS fault bits as debounced alarms.
- `combined_battery_dbus.py` publishes the whole bank as
  `com.victronenergy.battery.felicity_combined` (default device instance 512),
  the service to select as battery monitor in the GX device.

Background on the BMS registers, fault codes and quirks:
[felicity-bms-notes](https://github.com/sonniboi/felicity-bms-notes).

> **Disclaimer.** This software feeds values into the control loop of a battery
> system (DVCC, ESS, inverters). Wrong values can lead to over-charging,
> deep-discharging or unexpected shutdowns. It is provided as is, without any
> warranty (see [LICENSE](LICENSE)). The BMS remains the only protection device:
> never rely on this software for safety. Test on your own system and keep the
> BMS limits correctly configured.

## Architecture

```
Felicity packs ──RS485/Modbus──> pack API (local HTTP, not included)
                                   │  GET /api/battery<N>.json
                                   ▼
                     battery_dbus_bridge.py <N>   (one process per pack, 3 s)
                     combined_battery_dbus.py     (one process, 5 s)
                                   │
                                   ▼  D-Bus (velib_python / vedbus)
                     Venus OS: GUI, VRM, DVCC, systemcalc, dbus-mqtt
```

The bridges only **read**. They never write to the BMS.

The pack API is a separate service that reads the packs over Modbus. It is not
part of this repository; its JSON format is documented in
[docs/api-contract.md](docs/api-contract.md), so any service that serves this
format can be used.

## How the bank is aggregated

| Value | Rule |
|---|---|
| SOC | weighted by capacity (`RatedCapacity`); an arithmetic mean under-weights a large pack |
| Voltage | mean of the packs (all on the same bus) |
| Current, power | sum; Victron convention, positive = charging |
| Temperature | mean of sensors 1–4 of all packs; min/max over the same sensors |
| Cells | numbered consecutively: pack 1 = 1–16, pack 2 = 17–32, … |
| Cycles | maximum of the packs |

Robustness rules, each from an observed failure:

- **No service with made-up zeros.** Services are created with `register=False`
  and measurements start as `None` (D-Bus "invalid"). The bank registers only
  after the first valid values are published. Otherwise every restart shows up
  in VRM/Home Assistant (via dbus-mqtt) as a SOC drop to 0 %. If no data arrives
  within `register_timeout` (90 s) the bank registers anyway, with invalid values
  but never 0, so an external "unavailable" watchdog cannot loop.
- **SOC 0 is invalid.** A pack reports 0 before its first Modbus read; in
  operation the BMS disconnects long before 0 %. SOC 0 is never published.
- **Packs without live data are ignored.** A pack that only delivers static
  fields (serial, capacity) would enter the weighting with SOC 0.
- **Short pack outages do not move the bank SOC.** The last valid SOC of a pack
  is carried forward for `pack_soc_ttl` (900 s); then the pack leaves the weighting.
- **Debounced faults.** A BMS fault is raised on D-Bus only after it was present
  in 20 consecutive polls (60 s); it is cleared immediately.
- **Required packs.** The bank is published only while the packs listed in
  `required` answer; the other packs are optional.

## Installation on Venus OS

Requirements: Venus OS with root access (large or normal image), a running pack
API. The bridges use only the Python standard library and the `vedbus` module
that ships with Venus OS.

```sh
mkdir -p /data/etc/felicity-bridge
# copy battery_dbus_bridge.py, combined_battery_dbus.py, felicity_config.py
cp config.example.ini /data/etc/felicity-bridge/config.ini   # then edit
```

Venus OS uses **daemontools**. One service directory per process, for example
`/data/etc/felicity-bridge/service-battery1/`:

```sh
# run
#!/bin/sh
exec 2>&1
exec python3 /data/etc/felicity-bridge/battery_dbus_bridge.py 1

# log/run   (svlogd does not exist on Venus OS - use multilog)
#!/bin/sh
exec multilog t s25000 n4 /var/log/felicity-battery1
```

The bank service is the same with `combined_battery_dbus.py` and no argument.
Make both files executable and link the directory into `/service`:

```sh
ln -s /data/etc/felicity-bridge/service-battery1 /service/felicity-battery1
```

`/service` is rebuilt at boot, so add the `ln -s` lines to `/data/rc.local`.
Files under `/data` survive firmware updates.

Check: `svstat /service/felicity-battery1`, log in `/var/log/felicity-battery1/current`,
values with `dbus -y com.victronenergy.battery.felicity_combined /Soc GetValue`.

## Configuration

`config.ini` is looked up in this order: `--config <file>`, environment variable
`FELICITY_BRIDGE_CONFIG`, `/data/etc/felicity-bridge/config.ini`, `config.ini`
next to the scripts. Every key is optional; see
[config.example.ini](config.example.ini).

| Section / key | Default | Meaning |
|---|---|---|
| `api.base_url` | `http://127.0.0.1:8080` | pack API |
| `api.timeout` | `5` | HTTP timeout, s |
| `api.poll_interval` | `3` | pack bridge interval, s |
| `venus.velib_path` | `/opt/victronenergy/dbus-systemcalc-py/ext/velib_python` | location of `vedbus` |
| `packs.numbers` | `1 2 3` | packs served by the API |
| `packs.required` | `1 2` | packs that must answer for the bank |
| `packs.cells_per_pack` | `16` | cells per pack |
| `packs.alarm_debounce_polls` | `20` | polls before a fault is raised |
| `pack.N.serial` | `felicity-N` | serial shown in GUI/VRM - keep stable |
| `pack.N.modbus_address` | `N` | bus address (informational, `/Mgmt/Connection`) |
| `pack.N.device_instance` | `N` | D-Bus device instance |
| `pack.N.product_name`, `custom_name` | `Felicity BMS Battery N`, `Felicity Battery N` | names |
| `pack.N.serial_port`, `firmware_version` | `/dev/ttyFelicityBMS`, `417` | shown until the first poll |
| `bank.device_instance` | `512` | device instance of the bank |
| `bank.product_name` | `Felicity Battery Bank (3x16S)` | derived from pack count |
| `bank.custom_name`, `serial`, `connection` | `Felicity Battery Bank`, `COMBINED_BANK`, `Aggregated from 2x Felicity BMS` | names |
| `bank.poll_interval` | `5` | bank interval, s |
| `bank.pack_soc_ttl` | `900` | carry-forward of a pack SOC, s |
| `bank.register_timeout` | `90` | register without data after, s |
| `bank.default_capacity` | `200` | Ah if a pack reports none |

⚠ Changing a serial number or device instance makes VRM and Home Assistant see a
new device.

## Limits

- Charge/discharge limits (CVL, CCL, DCL for DVCC) are **not** published; they
  are handled by a separate service.
- `/History/ChargedEnergy` and `/History/DischargedEnergy` are not published: the
  BMS registers behind them hold no usable energy counters.
- Tested on Venus OS 3.x with three packs; other Felicity models with the same
  register map should work but are untested.

## Development

```sh
python -m pip install pytest
python -m pytest
```

The tests replace `vedbus`, `dbus` and `gi` with fakes. `tests/test_golden_reference.py`
feeds API responses captured on a live system into the code and compares the
result with a D-Bus snapshot of the running services taken at the same time
(see [tests/fixtures/README.md](tests/fixtures/README.md)).

## License

MIT, see [LICENSE](LICENSE).
