# AirGo

Control a Miniware AirGo 140 hot air gun over Bluetooth LE: a Go library, a CLI, and a
Home Assistant integration. [PROTOCOL.md](PROTOCOL.md) documents the protocol and how far
each part has been verified.

> [!WARNING]
> These tools can remotely turn on a heater that reaches 450 °C. Only start it when
> someone is at the gun. The gun's standby timeouts are your backstop, and setting them
> to 0 appears to disable them.

## CLI

    go install github.com/lbsfilm/airgo/cmd/airgo@latest

    airgo scan
    airgo status
    airgo watch -json
    airgo set gear1.temp 280 gear1.fan 3 volume 2   # gun must be idle
    airgo heat -gear 1 -yes
    airgo target -temp 320 -fan 4                   # gun must be heating
    airgo stop

Use `-addr` or `-name AirGo140_7A16` before the command if you have more than one gun. On
macOS, allow Bluetooth for your terminal when prompted.

## Go library

```go
ctx := context.Background()
gun, err := airgo.Connect(ctx, airgo.Options{})
if err != nil {
	log.Fatal(err)
}
defer gun.Close()

fmt.Println(gun.State()[airgo.ParamTemperature])

err = gun.Set(ctx, airgo.Param{ID: airgo.ParamGear1Temp, Value: 280})

for {
	select {
	case s := <-gun.Updates():
		fmt.Println(s[airgo.ParamTemperature], s.Heating())
	case <-gun.Done():
		return // Connection lost: call airgo.Connect again.
	}
}
```

The wire format in `protocol.go` has no Bluetooth dependency.

## Home Assistant

[![Open in the Home Assistant Marketplace](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/marketplace_repository/?owner=lbsfilm&repository=airgo&category=integration)

Install it from the Marketplace (Settings > Marketplace, Home Assistant 2026.11 or newer)
with the button above. Alternatively, copy `custom_components/airgo140` into your
`config/custom_components` folder and restart. The gun is then discovered automatically,
or can be added under Settings > Devices & services. It needs a Bluetooth adapter or an active Bluetooth proxy
in range.

| Entities | Notes |
|---|---|
| Temperature, target, output, active gear, input voltage, PD power, heater resistance, firmware | Sensors |
| Heating, power supply too weak, heater fault | Binary sensors |
| Stop heating | Button |
| Target temperature, fan level | Numbers, available only while heating |
| Gear temperatures and fans, standby timeouts, power limits, brightness, volume, unit, cool mode, language, display off | Configuration, available only while idle |
| **Heat** (off / gear 1-5) | **Disabled by default** |

Remote start is opt-in: enable the *Heat* select on the device page if you want it. Think
twice before using it in automations. Factory reset is deliberately not exposed.

Home Assistant keeps a connection open whenever the gun is on, so expect the phone app to
be unable to connect at the same time. An unplugged gun simply shows as unavailable and
reconnects when it returns.

## Development

    go test ./...

    uv venv --python 3.14 .venv
    uv pip install --python .venv/bin/python pytest-homeassistant-custom-component
    .venv/bin/python -m pytest tests

Home Assistant's Bluetooth component requirements must also be installed; they are listed
in each component's `manifest.json`. `AIRGO_LIVE=1` additionally runs `tests/test_live.py`
against a real gun. It only reads and sends commands the idle gun rejects.
