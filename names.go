package airgo

import "fmt"

var paramNames = map[ParamID]string{
	ParamDeviceCode:      "device-code",
	ParamFirmwareVersion: "firmware",
	ParamHardwareVersion: "hardware",
	ParamPDPower:         "pd-power-w",
	ParamInputVoltage:    "input-mv",
	ParamHeaterMilliOhm:  "heater-mohm",
	ParamTemperature:     "temperature",
	ParamTimerA:          "timer-a",
	ParamFlags:           "flags",
	ParamCounterB:        "counter-b",
	ParamActiveGear:      "active-gear",
	ParamTargetTemp:      "target-temp",
	ParamFanLevel:        "fan-level",
	ParamHeatValueC:      "heat-value-c",
	ParamOutputPercent:   "output-percent",
	ParamGear1Temp:       "gear1.temp",
	ParamGear1Fan:        "gear1.fan",
	ParamGear2Temp:       "gear2.temp",
	ParamGear2Fan:        "gear2.fan",
	ParamGear3Temp:       "gear3.temp",
	ParamGear3Fan:        "gear3.fan",
	ParamGear4Temp:       "gear4.temp",
	ParamGear4Fan:        "gear4.fan",
	ParamGear5Temp:       "gear5.temp",
	ParamGear5Fan:        "gear5.fan",
	ParamIdleTime:        "idle-time",
	ParamIdleDetect:      "idle-detect",
	ParamTempUnit:        "temp-unit",
	ParamPDPowerLimit:    "pd-limit",
	ParamDCPowerLimit:    "dc-limit",
	ParamFastCool:        "fast-cool",
	ParamBacklight:       "backlight",
	ParamVolume:          "volume",
	ParamLanguage:        "language",
	ParamScreenOff:       "screen-off",
}

// Name returns the stable name of a parameter.
func (id ParamID) Name() string {
	if n, ok := paramNames[id]; ok {
		return n
	}
	return fmt.Sprintf("param-%d", uint16(id))
}

// SettingByName finds a stored setting by Name.
func SettingByName(name string) (ParamID, bool) {
	for id, n := range paramNames {
		if n == name && id >= ParamGear1Temp {
			return id, true
		}
	}
	return 0, false
}

// Named renders the state keyed by parameter name.
func (s State) Named() map[string]int16 {
	out := make(map[string]int16, len(s))
	for id, v := range s {
		out[id.Name()] = v
	}
	return out
}
