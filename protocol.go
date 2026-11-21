// Package airgo talks to the Miniware AirGo 140 hot air gun over Bluetooth LE.
//
// This file holds the wire protocol and has no Bluetooth dependency. See
// PROTOCOL.md for the full description and how each detail was established.
package airgo

import (
	"encoding/binary"
	"errors"
	"fmt"
)

// ParamID identifies one 16-bit device parameter.
type ParamID uint16

// Live status parameters, read-only unless noted.
const (
	ParamDeviceCode      ParamID = 1  // Fixed 104 in firmware; meaning unknown.
	ParamFirmwareVersion ParamID = 2  // Digits of the version string: v1.22 = 122, v1.3 = 13.
	ParamHardwareVersion ParamID = 3  // Probably hardware version x100.
	ParamPDPower         ParamID = 4  // Negotiated USB-PD power in W, 0 when not on PD.
	ParamInputVoltage    ParamID = 5  // Supply voltage in mV.
	ParamHeaterMilliOhm  ParamID = 6  // Heating element resistance in mOhm, Unavailable if out of range.
	ParamTemperature     ParamID = 7  // Current temperature in the configured unit.
	ParamTimerA          ParamID = 8  // Unidentified timer, reset when heating starts.
	ParamFlags           ParamID = 9  // Status bit field, see the Flag constants.
	ParamCounterB        ParamID = 10 // Unidentified persisted counter.
	ParamActiveGear      ParamID = 11 // 0 when not heating, otherwise gear 1-5.
	ParamTargetTemp      ParamID = 12 // Target temperature; writable while heating.
	ParamFanLevel        ParamID = 13 // Fan level 1-5; writable while heating.
	ParamHeatValueC      ParamID = 14 // Unidentified signed value, only valid while heating.
	ParamOutputPercent   ParamID = 15 // Heater output 0-100 %, only valid while heating.
)

// Stored settings, writable only while the gun is not heating.
const (
	ParamGear1Temp    ParamID = 16
	ParamGear1Fan     ParamID = 17
	ParamGear2Temp    ParamID = 18
	ParamGear2Fan     ParamID = 19
	ParamGear3Temp    ParamID = 20
	ParamGear3Fan     ParamID = 21
	ParamGear4Temp    ParamID = 22
	ParamGear4Fan     ParamID = 23
	ParamGear5Temp    ParamID = 24
	ParamGear5Fan     ParamID = 25
	ParamIdleTime     ParamID = 26 // Auto standby timeout, 0-30.
	ParamIdleDetect   ParamID = 27 // Inactivity standby timeout, 0-30.
	ParamTempUnit     ParamID = 28 // 0 = Celsius, 1 = Fahrenheit.
	ParamPDPowerLimit ParamID = 29 // USB-PD power limit in W, 40-140.
	ParamDCPowerLimit ParamID = 30 // DC power limit in W, 40-140.
	ParamFastCool     ParamID = 31 // 0 = default, 1 = fast.
	ParamBacklight    ParamID = 32 // 1-10.
	ParamVolume       ParamID = 33 // 0-5.
	ParamLanguage     ParamID = 34 // 0 = EN, 1 = CN, 2 = DE, 3 = RU.
	ParamScreenOff    ParamID = 35 // 1 = turn display off after 10 min standby.
)

// Bits of ParamFlags. The low byte holds an error page code (see ErrorCode).
const (
	FlagHeating      uint16 = 1 << 15 // Heating session active.
	FlagLowPower     uint16 = 1 << 14 // Power supply too weak to heat.
	FlagHeaterFault  uint16 = 1 << 13 // Heater resistance outside 3-5 Ohm.
	FlagHeaterSensed uint16 = 1 << 12 // Heater current detected (probable meaning).
)

// Unavailable is the sentinel the gun reports for values with no current meaning.
const Unavailable int16 = -5000

// MaxParamsPerWrite is the number of parameters the firmware accepts per write:
// it copies a fixed 92 bytes (size + count + 20 entries + CRC).
const MaxParamsPerWrite = 20

// Command is a short action written to characteristic FFE2.
type Command uint32

const (
	CommandGear1  Command = 1 // Start heating on gear 1, or switch to it while heating.
	CommandGear2  Command = 2
	CommandGear3  Command = 3
	CommandGear4  Command = 4
	CommandGear5  Command = 5
	CommandStop   Command = 6 // Stop heating.
	CommandReset  Command = 7 // Restore factory settings, only accepted when idle.
	ackRejected           = 8 // Ack argument: command not valid in the current state.
	frameParams           = 1
	frameCmdAck           = 3
	frameSetError         = 4
)

// SetError is the reason the gun rejected a parameter write.
type SetError uint32

const (
	SetErrorWrongState SetError = 2 // Settings need idle; target temp and fan need heating.
	SetErrorUnknownID  SetError = 3
	SetErrorBadValue   SetError = 4
)

func (e SetError) Error() string {
	switch e {
	case SetErrorWrongState:
		return "airgo: parameter cannot be written in the current state (settings need idle, target temp and fan need heating)"
	case SetErrorUnknownID:
		return "airgo: unknown parameter id"
	case SetErrorBadValue:
		return "airgo: value out of range"
	}
	return fmt.Sprintf("airgo: parameter write rejected (code %d)", uint32(e))
}

// Param is one id/value pair.
type Param struct {
	ID    ParamID
	Value int16
}

// Frame errors.
var (
	ErrShortFrame = errors.New("airgo: frame too short or misaligned")
	ErrBadCRC     = errors.New("airgo: frame CRC mismatch")
	ErrBadLength  = errors.New("airgo: frame length field does not match")
	ErrCount      = errors.New("airgo: parameter count does not fit the frame")
)

// CRC computes the checksum used by every frame: the CH32/STM32 hardware CRC
// unit (CRC-32/MPEG-2) fed little-endian 32-bit words.
func CRC(data []byte) uint32 {
	c := uint32(0xFFFFFFFF)
	for i := 0; i+4 <= len(data); i += 4 {
		c ^= binary.LittleEndian.Uint32(data[i:])
		for b := 0; b < 32; b++ {
			if c&0x80000000 != 0 {
				c = c<<1 ^ 0x04C11DB7
			} else {
				c <<= 1
			}
		}
	}
	return c
}

// validated returns the frame without its CRC after checking length and CRC.
func validated(b []byte) ([]byte, error) {
	if len(b) < 8 || len(b)%4 != 0 {
		return nil, ErrShortFrame
	}
	size := binary.LittleEndian.Uint32(b)
	if size < 8 || size%4 != 0 || int(size) > len(b) {
		return nil, ErrBadLength
	}
	body := b[:size-4]
	if CRC(body) != binary.LittleEndian.Uint32(b[size-4:]) {
		return nil, ErrBadCRC
	}
	return body, nil
}

func decodeParams(b []byte, count uint32) ([]Param, error) {
	if uint64(count)*4 != uint64(len(b)) {
		return nil, ErrCount
	}
	out := make([]Param, count)
	for i := range out {
		out[i].Value = int16(binary.LittleEndian.Uint16(b[i*4:]))
		out[i].ID = ParamID(binary.LittleEndian.Uint16(b[i*4+2:]))
	}
	return out, nil
}

// DecodeRead parses the frame returned by reading FFE1:
// size, count, entries, CRC.
func DecodeRead(b []byte) ([]Param, error) {
	body, err := validated(b)
	if err != nil {
		return nil, err
	}
	if len(body) < 8 {
		return nil, ErrShortFrame
	}
	return decodeParams(body[8:], binary.LittleEndian.Uint32(body[4:]))
}

// Notification is a decoded FFE3 frame. Exactly one of the groups is set:
// Params for parameter updates, Ack for a command result, SetErr for a rejected
// parameter write.
type Notification struct {
	Params []Param
	Ack    *CommandAck
	SetErr SetError
}

// CommandAck reports the result of a command.
type CommandAck struct {
	Command  Command // Set when accepted.
	Rejected bool    // The command is not valid in the current state.
}

// DecodeNotification parses a frame received on FFE3.
func DecodeNotification(b []byte) (Notification, error) {
	var n Notification
	body, err := validated(b)
	if err != nil {
		return n, err
	}
	if len(body) < 12 {
		return n, ErrShortFrame
	}
	kind := binary.LittleEndian.Uint32(body[4:])
	arg := binary.LittleEndian.Uint32(body[8:])
	switch kind {
	case frameParams:
		n.Params, err = decodeParams(body[12:], arg)
		return n, err
	case frameCmdAck:
		if arg == ackRejected {
			n.Ack = &CommandAck{Rejected: true}
		} else {
			n.Ack = &CommandAck{Command: Command(arg)}
		}
		return n, nil
	case frameSetError:
		n.SetErr = SetError(arg)
		return n, nil
	}
	return n, fmt.Errorf("airgo: unknown frame type %d", kind)
}

// EncodeCommand builds the 12-byte frame for FFE2.
func EncodeCommand(c Command) []byte {
	b := make([]byte, 12)
	binary.LittleEndian.PutUint32(b, 12)
	binary.LittleEndian.PutUint32(b[4:], uint32(c))
	binary.LittleEndian.PutUint32(b[8:], CRC(b[:8]))
	return b
}

// EncodeParams builds the frame for writing FFE1: size, count, entries, CRC.
func EncodeParams(params []Param) ([]byte, error) {
	if len(params) == 0 || len(params) > MaxParamsPerWrite {
		return nil, fmt.Errorf("airgo: %d parameters in one write, need 1-%d", len(params), MaxParamsPerWrite)
	}
	size := 8 + 4*len(params) + 4
	b := make([]byte, size)
	binary.LittleEndian.PutUint32(b, uint32(size))
	binary.LittleEndian.PutUint32(b[4:], uint32(len(params)))
	for i, p := range params {
		binary.LittleEndian.PutUint16(b[8+i*4:], uint16(p.Value))
		binary.LittleEndian.PutUint16(b[10+i*4:], uint16(p.ID))
	}
	binary.LittleEndian.PutUint32(b[size-4:], CRC(b[:size-4]))
	return b, nil
}

// GearTempParam returns the stored temperature parameter of gear 1-5.
func GearTempParam(gear int) ParamID { return ParamGear1Temp + ParamID(gear-1)*2 }

// GearFanParam returns the stored fan parameter of gear 1-5.
func GearFanParam(gear int) ParamID { return ParamGear1Fan + ParamID(gear-1)*2 }

// CheckValue reports whether v is a legal value for a writable parameter.
//
// The firmware only range-checks target temperature and fan level. Stored
// settings are written to flash unchecked, so this guard is the only protection
// against storing nonsense. fahrenheit selects the temperature range.
func CheckValue(id ParamID, v int16, fahrenheit bool) error {
	lo, hi := int16(0), int16(0)
	switch {
	case id == ParamTargetTemp, id >= ParamGear1Temp && id <= ParamGear5Fan && (id-ParamGear1Temp)%2 == 0:
		lo, hi = 80, 450
		if fahrenheit {
			lo, hi = 180, 840
		}
	case id == ParamFanLevel, id >= ParamGear1Temp && id <= ParamGear5Fan:
		lo, hi = 1, 5
	case id == ParamIdleTime, id == ParamIdleDetect:
		lo, hi = 0, 30
	case id == ParamTempUnit, id == ParamFastCool, id == ParamScreenOff:
		lo, hi = 0, 1
	case id == ParamPDPowerLimit, id == ParamDCPowerLimit:
		lo, hi = 40, 140
	case id == ParamBacklight:
		lo, hi = 1, 10
	case id == ParamVolume:
		lo, hi = 0, 5
	case id == ParamLanguage:
		lo, hi = 0, 3
	default:
		return fmt.Errorf("airgo: parameter %d is not writable", id)
	}
	if v < lo || v > hi {
		return fmt.Errorf("airgo: value %d for parameter %d outside %d-%d", v, id, lo, hi)
	}
	return nil
}

// State is the merged view of all parameters seen so far.
type State map[ParamID]int16

// Apply merges updates into the state.
func (s State) Apply(params []Param) {
	for _, p := range params {
		s[p.ID] = p.Value
	}
}

// Heating reports whether a heating session is active.
func (s State) Heating() bool { return s[ParamActiveGear] != 0 }

// Fahrenheit reports whether temperatures are in Fahrenheit.
func (s State) Fahrenheit() bool { return s[ParamTempUnit] == 1 }

// ErrorCode returns the gun's error page code, 0 when none.
func (s State) ErrorCode() uint8 { return uint8(uint16(s[ParamFlags])) }

// FirmwareString renders ParamFirmwareVersion: 122 becomes "1.22", 13 becomes "1.3".
func (s State) FirmwareString() string {
	d := fmt.Sprint(s[ParamFirmwareVersion])
	if len(d) < 2 {
		return d
	}
	return d[:1] + "." + d[1:]
}
