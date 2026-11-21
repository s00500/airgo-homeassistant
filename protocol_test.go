package airgo

import (
	"encoding/binary"
	"errors"
	"testing"
)

// Values and CRCs captured from a real AirGo 140 (firmware 122).
var capturedRead = []Param{
	{1, 104}, {2, 122}, {3, 101}, {4, 140}, {5, 27745}, {6, 3608}, {7, 19}, {8, 0}, {9, 0}, {10, 0}, {11, 0},
	{12, -5000}, {13, -5000}, {14, -5000}, {15, -5000},
	{16, 250}, {17, 3}, {18, 300}, {19, 4}, {20, 360}, {21, 4}, {22, 300}, {23, 2}, {24, 120}, {25, 5},
	{26, 5}, {27, 0}, {28, 0}, {29, 140}, {30, 140}, {31, 1}, {32, 5}, {33, 4}, {34, 0}, {35, 0},
}

const capturedReadCRC = 0x7cf9fe24

var capturedNotify = []Param{
	{1, 104}, {2, 122}, {3, 101}, {4, 140}, {5, 27753}, {6, 3625}, {7, 18}, {8, 0}, {9, 0}, {10, 0}, {11, 0},
	{12, -5000}, {13, -5000}, {14, -5000}, {15, -5000},
	{16, 250}, {17, 3}, {18, 300}, {19, 4}, {20, 360}, {21, 4}, {22, 300}, {23, 2}, {24, 120}, {25, 5},
	{26, 5}, {27, 0}, {28, 0}, {29, 140}, {30, 140}, {31, 1}, {32, 5}, {33, 4}, {34, 0}, {35, 0},
}

const capturedNotifyCRC = 0x7b747d9d

func buildRead(p []Param) []byte {
	b := make([]byte, 8+4*len(p)+4)
	binary.LittleEndian.PutUint32(b, uint32(len(b)))
	binary.LittleEndian.PutUint32(b[4:], uint32(len(p)))
	for i, e := range p {
		binary.LittleEndian.PutUint16(b[8+i*4:], uint16(e.Value))
		binary.LittleEndian.PutUint16(b[10+i*4:], uint16(e.ID))
	}
	return b
}

func TestCRCMatchesCapturedRead(t *testing.T) {
	b := buildRead(capturedRead)
	if got := CRC(b[:len(b)-4]); got != capturedReadCRC {
		t.Fatalf("CRC = %08x, want %08x", got, capturedReadCRC)
	}
}

func TestDecodeCapturedRead(t *testing.T) {
	b := buildRead(capturedRead)
	binary.LittleEndian.PutUint32(b[len(b)-4:], capturedReadCRC)
	got, err := DecodeRead(b)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 35 || got[4] != (Param{ParamInputVoltage, 27745}) || got[11] != (Param{ParamTargetTemp, Unavailable}) {
		t.Fatalf("decoded %v", got)
	}
}

func TestDecodeCapturedNotification(t *testing.T) {
	p := capturedNotify
	b := make([]byte, 12+4*len(p)+4)
	binary.LittleEndian.PutUint32(b, uint32(len(b)))
	binary.LittleEndian.PutUint32(b[4:], frameParams)
	binary.LittleEndian.PutUint32(b[8:], uint32(len(p)))
	for i, e := range p {
		binary.LittleEndian.PutUint16(b[12+i*4:], uint16(e.Value))
		binary.LittleEndian.PutUint16(b[14+i*4:], uint16(e.ID))
	}
	binary.LittleEndian.PutUint32(b[len(b)-4:], capturedNotifyCRC)
	n, err := DecodeNotification(b)
	if err != nil {
		t.Fatal(err)
	}
	s := State{}
	s.Apply(n.Params)
	if s[ParamTemperature] != 18 || s.Heating() || s.Fahrenheit() || s.FirmwareString() != "1.22" {
		t.Fatalf("state %v", s)
	}
}

func shortFrame(kind, arg uint32) []byte {
	b := make([]byte, 16)
	binary.LittleEndian.PutUint32(b, 16)
	binary.LittleEndian.PutUint32(b[4:], kind)
	binary.LittleEndian.PutUint32(b[8:], arg)
	binary.LittleEndian.PutUint32(b[12:], CRC(b[:12]))
	return b
}

func TestDecodeAcksAndErrors(t *testing.T) {
	n, err := DecodeNotification(shortFrame(frameCmdAck, uint32(CommandStop)))
	if err != nil || n.Ack == nil || n.Ack.Rejected || n.Ack.Command != CommandStop {
		t.Fatalf("ack: %+v %v", n, err)
	}
	n, err = DecodeNotification(shortFrame(frameCmdAck, ackRejected))
	if err != nil || n.Ack == nil || !n.Ack.Rejected {
		t.Fatalf("reject: %+v %v", n, err)
	}
	n, err = DecodeNotification(shortFrame(frameSetError, uint32(SetErrorWrongState)))
	if err != nil || n.SetErr != SetErrorWrongState {
		t.Fatalf("set error: %+v %v", n, err)
	}
	if _, err = DecodeNotification(shortFrame(99, 0)); err == nil {
		t.Fatal("unknown frame type accepted")
	}
}

func TestDecodeRejectsCorruption(t *testing.T) {
	b := shortFrame(frameCmdAck, 1)
	b[8] ^= 1
	if _, err := DecodeNotification(b); !errors.Is(err, ErrBadCRC) {
		t.Fatalf("bit flip: %v", err)
	}
	if _, err := DecodeNotification(b[:15]); !errors.Is(err, ErrShortFrame) {
		t.Fatalf("misaligned: %v", err)
	}
	long := shortFrame(frameCmdAck, 1)
	binary.LittleEndian.PutUint32(long, 400)
	if _, err := DecodeNotification(long); !errors.Is(err, ErrBadLength) {
		t.Fatalf("size: %v", err)
	}
	// A count that disagrees with the frame size must not over-read.
	bad := make([]byte, 16)
	binary.LittleEndian.PutUint32(bad, 16)
	binary.LittleEndian.PutUint32(bad[4:], 0xFFFFFFFF)
	binary.LittleEndian.PutUint32(bad[12:], CRC(bad[:12]))
	if _, err := DecodeRead(bad); !errors.Is(err, ErrCount) {
		t.Fatalf("count: %v", err)
	}
}

func TestEncodeRoundTrip(t *testing.T) {
	in := []Param{{ParamTargetTemp, 300}, {ParamFanLevel, 3}}
	b, err := EncodeParams(in)
	if err != nil {
		t.Fatal(err)
	}
	if len(b) != 20 || binary.LittleEndian.Uint32(b) != 20 {
		t.Fatalf("size: %x", b)
	}
	out, err := DecodeRead(b)
	if err != nil || len(out) != 2 || out[0] != in[0] || out[1] != in[1] {
		t.Fatalf("round trip %v %v", out, err)
	}
	if _, err := EncodeParams(make([]Param, MaxParamsPerWrite+1)); err == nil {
		t.Fatal("oversized write accepted")
	}
	c := EncodeCommand(CommandStop)
	if len(c) != 12 || binary.LittleEndian.Uint32(c[4:]) != 6 || CRC(c[:8]) != binary.LittleEndian.Uint32(c[8:]) {
		t.Fatalf("command %x", c)
	}
}

func TestCheckValue(t *testing.T) {
	cases := []struct {
		id ParamID
		v  int16
		f  bool
		ok bool
	}{
		{ParamGear1Temp, 80, false, true}, {ParamGear1Temp, 79, false, false}, {ParamGear5Temp, 451, false, false},
		{ParamGear3Temp, 840, true, true}, {ParamGear3Temp, 841, true, false},
		{ParamGear1Fan, 1, false, true}, {ParamGear5Fan, 6, false, false}, {ParamGear5Fan, 0, false, false},
		{ParamTargetTemp, 450, false, true}, {ParamFanLevel, 5, false, true}, {ParamFanLevel, 6, false, false},
		{ParamIdleTime, 30, false, true}, {ParamIdleDetect, 31, false, false},
		{ParamPDPowerLimit, 39, false, false}, {ParamDCPowerLimit, 140, false, true},
		{ParamBacklight, 0, false, false}, {ParamVolume, 5, false, true}, {ParamLanguage, 4, false, false},
		{ParamTemperature, 20, false, false}, {ParamActiveGear, 1, false, false},
	}
	for _, c := range cases {
		if err := CheckValue(c.id, c.v, c.f); (err == nil) != c.ok {
			t.Errorf("CheckValue(%d, %d, %v) = %v", c.id, c.v, c.f, err)
		}
	}
	if GearTempParam(3) != ParamGear3Temp || GearFanParam(5) != ParamGear5Fan {
		t.Error("gear parameter mapping")
	}
}
