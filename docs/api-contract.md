# Pack API contract

The bridges do not talk to the BMS themselves. They poll a local HTTP service
(not part of this repository) that reads the packs over RS485/Modbus and serves
one JSON document per pack:

```
GET <base_url>/api/battery<N>.json      N = pack number (1, 2, 3, ...)
```

Every field is an object with the value and a display unit:

```json
"BatteryVoltage": {"value": 53.07, "unit": "V"}
```

A missing field is skipped; the D-Bus path keeps its previous value. Real
responses captured on a live system are in `tests/fixtures/api_battery*.json`.

## Fields used by the bridges

| Field | Type | Unit | D-Bus path (pack service) | Used by bank |
|---|---|---|---|---|
| `SOC` | number | % | `/Soc` (only if 0 < SOC <= 100) | weighted SOC |
| `BatteryVoltage` | number | V | `/Dc/0/Voltage`, `/Dc/0/Power` | mean voltage |
| `BatteryCurrent` | number | A, + = charging | `/Dc/0/Current`, `/Dc/0/Power` | summed current |
| `RatedCapacity` | number | Ah | `/InstalledCapacity` | SOC weighting, capacity |
| `Cell1 Voltage` … `Cell16 Voltage` | number | V | `/Voltages/Cell1..16`, min/max | cells 1..48, min/max |
| `Sensor1 Temperature` … `Sensor8 Temperature` | number | °C | `/System/Temperature1..8` | sensors 1..4: mean, min, max |
| `MaxCellVoltageId`, `MinCellVoltageId` | integer | | `/System/Max/MinVoltageCellId` | |
| `MaxCellTemperature`, `MinCellTemperature` | number | °C | `/System/Max/MinCellTemperature` | |
| `MaxCellTemperatureId`, `MinCellTemperatureId` | integer | | `/System/Max/MinTemperatureCellId` | |
| `SOH` | number | % | `/Soh` | |
| `CycleCount` | integer | cycles | `/History/ChargeCycles` | maximum |
| `BMSUptime` | integer | h | `/System/BMSUptime` | |
| `BMSVersion` | integer/string | | `/FirmwareVersion` | |
| `MOSTemperature` | number | °C | `/System/MOSTemperature` | |
| `FaultStatus` | string, `"None"` if ok | | `/Alarms/*` (debounced) | |
| `FaultRaw` | integer | | `/Alarms/FaultRaw` | |
| `ChargeEnabled`, `DischargeEnabled` | `"Yes"` / `"No"` | | `/Alarms/ChargeEnabled`, `/Alarms/DischargeEnabled` | |

`FaultStatus` is matched case-insensitively against these texts: `cell v high`,
`cell v low`, `charge i high`, `discharge i high`, `mos temp high`,
`cell temp high`, `mos temp low`, `cell temp low` and the fault codes `C01`,
`C02`, `C08`, `C11`, `C12`, `C13`. The meaning of the codes is documented in
[felicity-bms-notes](https://github.com/sonniboi/felicity-bms-notes).

## Fields present but not used

`AlarmRaw`, `AlarmStatus`, `BMSTemperature`, `BatteryModel`, `CellVoltageDelta`,
`ChargeCurrentLimit`, `ChargeVoltageLimit`, `DischargeCurrentLimit`,
`DischargeVoltageLimit`, `ChargeImmediately`, `FaultCode`, `FaultCodeText`,
`FirmwareBuild`, `MCU2Version`, `MaxCellVoltage`, `MinCellVoltage`, `SerialNumber`.

The charge/discharge limits are deliberately not published: charge voltage and
current limits (DVCC) are handled by a separate service.
