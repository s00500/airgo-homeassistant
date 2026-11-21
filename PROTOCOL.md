# AirGo 140 BLE protocol

Reverse engineered from the official firmware image `AirGo140APPV130.zip`
(v1.3, plain Intel HEX for a WCH CH32F20x with on-chip BLE), then checked against a
real gun running firmware 1.22. The phone apps were not analysed.

Status of each claim:

- **Live**: observed on the real gun.
- **Firmware**: read from the disassembly, not yet observed. Everything that needs the
  gun to be heating is in this group.

## GATT

The gun advertises as `AirGo140_XXXX` with service `FFE0`. No pairing is required; the
characteristics need no encryption. (Live)

| Characteristic | Properties | Use |
|---|---|---|
| `FFE1` | read, write | Read all parameters / write parameters |
| `FFE2` | write | Commands |
| `FFE3` | notify | Status and answers |

Both writes need write-with-response. Device Information (`180A`) exists but only holds
placeholder strings such as `Model Number`. (Live)

The firmware refuses long reads and long writes, so frames must fit one ATT packet. A
full read needs an MTU of at least 155, the full notification 159, and the status
notification 79. macOS negotiated 247. (MTU values: Firmware)

## Framing

All integers are little-endian. Every frame is a multiple of 4 bytes and starts with its
total size in bytes:

    u32 size | payload | u32 crc

The CRC covers everything before it, as 32-bit little-endian words: CRC-32/MPEG-2
(polynomial `04C11DB7`, init `FFFFFFFF`, no reflection, no final XOR), as computed by the
chip's CRC unit. (Live: matches captured frames)

A parameter entry is `i16 value | u16 id`.

| Direction | Frame | Layout |
|---|---|---|
| Read `FFE1` | All parameters | `size=152, count=35, entries, crc` |
| Write `FFE1` | Set parameters | `size, count (max 20), entries, crc` |
| Write `FFE2` | Command | `size=12, command, crc` |
| Notify `FFE3` | Parameters | `size, type=1, count, entries, crc` |
| Notify `FFE3` | Command answer | `size=16, type=3, arg, crc` |
| Notify `FFE3` | Write rejected | `size=16, type=4, code, crc` |

Subscribing triggers one notification with all 35 parameters, then IDs 1-15 arrive about
once per second. Changing a setting on the gun itself sends a one-entry parameter frame.
(Live for the full and status frames; Firmware for the one-entry frame)

## Timing

**Answers are slow.** Command answers and write rejections were measured arriving up to
2.5 s after the write, and a successful parameter write sends nothing at all. Silence
therefore proves nothing. The firmware applies a write before responding to it, so read
`FFE1` back to learn the outcome, and wait about 4 s before concluding a frame is not
coming. (Live)

## Commands (`FFE2`)

| Command | Meaning | Accepted | Answer | Status |
|---|---|---|---|---|
| 1-5 | Start heating on that gear | Idle | `arg` = command | Firmware |
| 1-5 | Switch gear | Heating | `arg` = command, or none if refused | Firmware |
| 6 | Stop heating | Heating | `arg` = 6 | Firmware |
| 7 | **Factory reset** and save | Idle | `arg` = 7 | Firmware |

In any other state the answer is `arg = 8`. (Live for stop while idle)

Starting with a power supply that is too weak sends **no answer**. The gun shows an error
page instead (Firmware).

## Parameter write errors (type 4 frame)

| Code | Meaning | Status |
|---|---|---|
| 2 | Wrong state: IDs 12-13 need heating, IDs 16-35 need idle | Live |
| 3 | ID cannot be written | Firmware |
| 4 | IDs 12-13 out of range | Firmware |

**The firmware does not range-check IDs 16-35.** They are stored to flash as sent, so a
client must validate them. Writing them makes the gun beep and save. (Live)

## Parameters

`-5000` means not applicable. Temperatures use the unit in ID 28.

### Live values

| ID | Meaning | Status |
|---|---|---|
| 1 | Fixed at 104, meaning unknown | Live value |
| 2 | Firmware version digits: 122 = v1.22, 13 = v1.3 | Live |
| 3 | Probably hardware version x100 (reads 101) | Unconfirmed |
| 4 | Negotiated USB-PD power in W, 0 without PD | Live |
| 5 | Input voltage in mV | Live |
| 6 | Heater resistance in mOhm, `-5000` outside 3-5 Ohm | Live |
| 7 | Current temperature | Live |
| 8 | Timer, reset when heating starts, capped at 60000; unit unknown | Firmware |
| 9 | Status flags, below | Firmware |
| 10 | Persisted counter, unit unknown | Firmware |
| 11 | Active gear 1-5, 0 when not heating | Live for 0 |
| 12 | Target temperature, writable while heating (80-450 °C, 180-840 °F) | Firmware |
| 13 | Fan level, writable while heating (1-5) | Firmware |
| 14 | Signed value, unidentified | Firmware |
| 15 | Heater output 0-100 % | Firmware |

Flags in ID 9: bit 15 heating, bit 14 power supply too weak, bit 13 heater fault, bit 12
probably heater current detected, low byte the error page code. (Firmware; all 0 when idle
is Live)

### Stored settings

Writable while idle. Values, ranges and defaults come from the firmware's settings table;
the live values read back consistently with it.

| ID | Setting | Range |
|---|---|---|
| 16, 18, 20, 22, 24 | Gear 1-5 temperature | 80-450 °C, 180-840 °F |
| 17, 19, 21, 23, 25 | Gear 1-5 fan level | 1-5 |
| 26 | Auto standby timeout, minutes | 0-30 |
| 27 | Inactivity standby timeout, minutes | 0-30 |
| 28 | Temperature unit: 0 °C, 1 °F | 0-1 |
| 29 | USB-PD power limit, W | 40-140 |
| 30 | DC power limit, W | 40-140 |
| 31 | Cool mode: 0 default, 1 fast | 0-1 |
| 32 | Display brightness | 1-10 |
| 33 | Volume | 0-5 |
| 34 | Language: EN, CN, DE, RU | 0-3 |
| 35 | Display off after 10 min standby | 0-1 |

The display direction / handedness setting exists on the gun but has no parameter ID.
