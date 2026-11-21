// Command airgo controls a Miniware AirGo 140 hot air gun over Bluetooth LE.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"time"

	"github.com/lbsfilm/airgo"
)

const usage = `Usage: airgo [-addr ADDRESS] [-name NAME] [-timeout 15s] COMMAND

Commands:
  scan                      List guns in range
  status [-json]            Print live values and all settings
  watch [-json]             Stream live values until interrupted
  set NAME VALUE [...]      Change stored settings (gun must be idle)
  target [-temp N] [-fan N] Change target temperature/fan (gun must be heating)
  heat -gear N -yes         Start heating, or switch gear while heating
  stop                      Stop heating
  factory-reset -yes        Restore factory settings (gun must be idle)

Settings: gear1.temp ... gear5.temp, gear1.fan ... gear5.fan, idle-time,
idle-detect, temp-unit, pd-limit, dc-limit, fast-cool, backlight, volume,
language, screen-off
`

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, "airgo:", strings.TrimPrefix(err.Error(), "airgo: "))
		os.Exit(1)
	}
}

func run() error {
	global := flag.NewFlagSet("airgo", flag.ExitOnError)
	global.Usage = func() { fmt.Fprint(os.Stderr, usage) }
	addr := global.String("addr", "", "device address")
	name := global.String("name", "", "device name, e.g. AirGo140_7A16")
	timeout := global.Duration("timeout", 15*time.Second, "time allowed to find the gun")
	_ = global.Parse(os.Args[1:])
	if global.NArg() == 0 {
		global.Usage()
		os.Exit(2)
	}
	cmd, args := global.Arg(0), global.Args()[1:]

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()

	connect := func() (*airgo.Client, error) {
		cctx, cancel := context.WithTimeout(ctx, *timeout)
		defer cancel()
		return airgo.Connect(cctx, airgo.Options{Address: *addr, Name: *name})
	}

	switch cmd {
	case "scan":
		return scan(ctx, *timeout)
	case "status":
		return withClient(connect, args, status)
	case "watch":
		return withClient(connect, args, func(c *airgo.Client, a []string) error { return watch(ctx, c, a) })
	case "set":
		return set(ctx, connect, args)
	case "target":
		return target(ctx, connect, args)
	case "heat":
		return heat(ctx, connect, args)
	case "stop":
		return withClient(connect, args, func(c *airgo.Client, _ []string) error {
			err := c.StopHeating(ctx)
			if errors.Is(err, airgo.ErrRejected) {
				return errors.New("gun is not heating")
			}
			if err == nil {
				fmt.Println("heating stopped")
			}
			return err
		})
	case "factory-reset":
		return factoryReset(ctx, connect, args)
	}
	global.Usage()
	return fmt.Errorf("unknown command %q", cmd)
}

func withClient(connect func() (*airgo.Client, error), args []string, f func(*airgo.Client, []string) error) error {
	c, err := connect()
	if err != nil {
		return err
	}
	defer c.Close()
	return f(c, args)
}

func scan(ctx context.Context, d time.Duration) error {
	cctx, cancel := context.WithTimeout(ctx, d)
	defer cancel()
	found, err := airgo.Scan(cctx)
	if err != nil {
		return err
	}
	if len(found) == 0 {
		return airgo.ErrNotFound
	}
	for _, f := range found {
		fmt.Printf("%s\t%s\t%d dBm\n", f.Name, f.Address, f.RSSI)
	}
	return nil
}

func printJSON(s airgo.State) error {
	return json.NewEncoder(os.Stdout).Encode(s.Named())
}

func unit(s airgo.State) string {
	if s.Fahrenheit() {
		return "°F"
	}
	return "°C"
}

func live(s airgo.State) string {
	line := fmt.Sprintf("temp %d%s  input %.2f V  ", s[airgo.ParamTemperature], unit(s), float64(s[airgo.ParamInputVoltage])/1000)
	if s[airgo.ParamPDPower] > 0 {
		line += fmt.Sprintf("PD %d W  ", s[airgo.ParamPDPower])
	}
	if s.Heating() {
		line += fmt.Sprintf("HEATING gear %d  target %d%s  fan %d  output %d%%",
			s[airgo.ParamActiveGear], s[airgo.ParamTargetTemp], unit(s), s[airgo.ParamFanLevel], s[airgo.ParamOutputPercent])
	} else {
		line += "idle"
	}
	flags := uint16(s[airgo.ParamFlags])
	if flags&airgo.FlagLowPower != 0 {
		line += "  LOW-POWER"
	}
	if flags&airgo.FlagHeaterFault != 0 {
		line += "  HEATER-FAULT"
	}
	if code := s.ErrorCode(); code != 0 {
		line += fmt.Sprintf("  error-page %d", code)
	}
	return line
}

func status(c *airgo.Client, args []string) error {
	fs := flag.NewFlagSet("status", flag.ExitOnError)
	asJSON := fs.Bool("json", false, "print JSON")
	_ = fs.Parse(args)
	s := c.State()
	if *asJSON {
		return printJSON(s)
	}
	fmt.Printf("%s  %s  firmware %s\n%s\n", c.Info().Name, c.Info().Address, s.FirmwareString(), live(s))
	if r := s[airgo.ParamHeaterMilliOhm]; r != airgo.Unavailable {
		fmt.Printf("heater %.3f Ohm\n", float64(r)/1000)
	}
	fmt.Println("\nGears:")
	for g := 1; g <= 5; g++ {
		fmt.Printf("  %d: %d%s  fan %d\n", g, s[airgo.GearTempParam(g)], unit(s), s[airgo.GearFanParam(g)])
	}
	fmt.Println("\nSettings:")
	for id := airgo.ParamIdleTime; id <= airgo.ParamScreenOff; id++ {
		fmt.Printf("  %-12s %d\n", id.Name(), s[id])
	}
	return nil
}

func watch(ctx context.Context, c *airgo.Client, args []string) error {
	fs := flag.NewFlagSet("watch", flag.ExitOnError)
	asJSON := fs.Bool("json", false, "print JSON lines")
	_ = fs.Parse(args)
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-c.Done():
			return airgo.ErrLinkLost
		case s := <-c.Updates():
			if *asJSON {
				if err := printJSON(s); err != nil {
					return err
				}
				continue
			}
			fmt.Printf("%s  %s\n", time.Now().Format("15:04:05"), live(s))
		}
	}
}

func set(ctx context.Context, connect func() (*airgo.Client, error), args []string) error {
	if len(args) == 0 || len(args)%2 != 0 {
		return errors.New("set needs NAME VALUE pairs")
	}
	var params []airgo.Param
	for i := 0; i < len(args); i += 2 {
		id, ok := airgo.SettingByName(args[i])
		if !ok {
			return fmt.Errorf("unknown setting %q", args[i])
		}
		v, err := strconv.ParseInt(args[i+1], 10, 16)
		if err != nil {
			return fmt.Errorf("value for %s: %w", args[i], err)
		}
		params = append(params, airgo.Param{ID: id, Value: int16(v)})
	}
	return withClient(connect, nil, func(c *airgo.Client, _ []string) error {
		if err := c.Set(ctx, params...); err != nil {
			return err
		}
		fmt.Println("saved and verified")
		return nil
	})
}

func target(ctx context.Context, connect func() (*airgo.Client, error), args []string) error {
	fs := flag.NewFlagSet("target", flag.ExitOnError)
	temp := fs.Int("temp", 0, "target temperature")
	fan := fs.Int("fan", 0, "fan level 1-5")
	_ = fs.Parse(args)
	return withClient(connect, nil, func(c *airgo.Client, _ []string) error {
		if err := c.SetTarget(ctx, int16(*temp), int16(*fan)); err != nil {
			return err
		}
		fmt.Println(live(c.State()))
		return nil
	})
}

func heat(ctx context.Context, connect func() (*airgo.Client, error), args []string) error {
	fs := flag.NewFlagSet("heat", flag.ExitOnError)
	gear := fs.Int("gear", 0, "gear 1-5")
	yes := fs.Bool("yes", false, "confirm: someone is at the gun")
	_ = fs.Parse(args)
	if *gear < 1 || *gear > 5 {
		return errors.New("heat needs -gear 1-5")
	}
	if !*yes {
		return errors.New("this turns on a heater of up to 450 °C remotely; pass -yes if someone is at the gun")
	}
	return withClient(connect, nil, func(c *airgo.Client, _ []string) error {
		err := c.StartHeating(ctx, *gear)
		switch {
		case errors.Is(err, airgo.ErrNoAck):
			_ = c.Refresh()
			return fmt.Errorf("gun did not start (often a too-weak power supply): %s", live(c.State()))
		case err != nil:
			return err
		}
		fmt.Println("heating started; `airgo stop` stops it")
		return nil
	})
}

func factoryReset(ctx context.Context, connect func() (*airgo.Client, error), args []string) error {
	fs := flag.NewFlagSet("factory-reset", flag.ExitOnError)
	yes := fs.Bool("yes", false, "confirm erasing all settings")
	_ = fs.Parse(args)
	if !*yes {
		return errors.New("this erases all gears and settings; pass -yes to confirm")
	}
	return withClient(connect, nil, func(c *airgo.Client, _ []string) error {
		if err := c.FactoryReset(ctx); err != nil {
			return err
		}
		fmt.Println("factory settings restored")
		return nil
	})
}
