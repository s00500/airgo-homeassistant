package airgo

import (
	"context"
	"errors"
	"fmt"
	"maps"
	"reflect"
	"strings"
	"sync"
	"time"

	"tinygo.org/x/bluetooth"
)

// NamePrefix is the advertised name prefix: the gun advertises as AirGo140_XXXX.
const NamePrefix = "AirGo140_"

var (
	serviceUUID = bluetooth.New16BitUUID(0xFFE0)
	paramsUUID  = bluetooth.New16BitUUID(0xFFE1)
	commandUUID = bluetooth.New16BitUUID(0xFFE2)
	notifyUUID  = bluetooth.New16BitUUID(0xFFE3)
)

const (
	// Answer frames were measured arriving up to 2.5 s after a write.
	replyTimeout = 4 * time.Second
	linkTimeout  = 5 * time.Second // The gun pushes status about once per second.
)

var (
	// ErrNotFound means no gun was seen during the scan.
	ErrNotFound = errors.New("airgo: no device found")
	// ErrRejected means the command is not valid in the current state.
	ErrRejected = errors.New("airgo: command rejected in the current state")
	// ErrNoAck means the gun neither answered a command nor shows its effect. This
	// is what starting heat looks like when the power supply is too weak, or when a
	// gear change is refused: the firmware then sends nothing. Check State for flags
	// and error code.
	ErrNoAck = errors.New("airgo: command not acknowledged")
	// ErrLinkLost means the BLE connection ended.
	ErrLinkLost = errors.New("airgo: connection lost")
)

// Found is a gun seen while scanning.
type Found struct {
	Name    string
	Address string // A MAC on Linux, a CoreBluetooth UUID on macOS.
	RSSI    int
}

var (
	adapter     = bluetooth.DefaultAdapter
	enableOnce  sync.Once
	enableErr   error
	activeMu    sync.Mutex
	activeOnEnd func(bluetooth.Device)
)

func enable() error {
	enableOnce.Do(func() {
		enableErr = adapter.Enable()
		adapter.SetConnectionStateHandler(func(d bluetooth.Device, state bluetooth.ConnectionState) {
			if state.Connected {
				return
			}
			activeMu.Lock()
			end := activeOnEnd
			activeMu.Unlock()
			if end != nil {
				end(d)
			}
		})
	})
	return enableErr
}

// scan reports guns until ctx ends or visit returns false.
func scan(ctx context.Context, visit func(Found, bluetooth.Address) bool) error {
	if err := enable(); err != nil {
		return fmt.Errorf("airgo: enable bluetooth: %w", err)
	}
	stop := context.AfterFunc(ctx, func() { _ = adapter.StopScan() })
	defer stop()
	return adapter.Scan(func(a *bluetooth.Adapter, r bluetooth.ScanResult) {
		if !strings.HasPrefix(r.LocalName(), NamePrefix) {
			return
		}
		f := Found{Name: r.LocalName(), Address: r.Address.String(), RSSI: int(r.RSSI)}
		if !visit(f, r.Address) {
			_ = a.StopScan()
		}
	})
}

// Scan lists guns seen until ctx ends.
func Scan(ctx context.Context) ([]Found, error) {
	seen := map[string]Found{}
	err := scan(ctx, func(f Found, _ bluetooth.Address) bool {
		seen[f.Address] = f
		return true
	})
	out := make([]Found, 0, len(seen))
	for _, f := range seen {
		out = append(out, f)
	}
	return out, err
}

// Client is a live connection to one gun.
type Client struct {
	dev     bluetooth.Device
	params  bluetooth.DeviceCharacteristic
	command bluetooth.DeviceCharacteristic
	notify  bluetooth.DeviceCharacteristic
	info    Found

	opMu sync.Mutex // Serializes GATT operations and their result channels.

	mu       sync.Mutex
	state    State
	lastSeen time.Time
	updates  chan State

	acks    chan CommandAck
	setErrs chan SetError

	done     chan struct{}
	doneOnce sync.Once
}

// Options selects which gun to connect to. The zero value picks the first seen.
type Options struct {
	Address string
	Name    string
}

// Connect finds a gun, connects and subscribes to its status stream.
func Connect(ctx context.Context, opt Options) (*Client, error) {
	var (
		info Found
		addr bluetooth.Address
	)
	scanCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	err := scan(scanCtx, func(f Found, a bluetooth.Address) bool {
		if (opt.Address != "" && !strings.EqualFold(opt.Address, f.Address)) || (opt.Name != "" && opt.Name != f.Name) {
			return true
		}
		info, addr = f, a
		return false
	})
	if err != nil {
		return nil, fmt.Errorf("airgo: scan: %w", err)
	}
	if info.Address == "" {
		if ctx.Err() != nil && !errors.Is(ctx.Err(), context.DeadlineExceeded) {
			return nil, ctx.Err()
		}
		return nil, ErrNotFound
	}

	dev, err := adapter.Connect(addr, bluetooth.ConnectionParams{})
	if err != nil {
		return nil, fmt.Errorf("airgo: connect %s: %w", info.Name, err)
	}
	// On macOS the library returns an empty device and no error when the link
	// fails during connect, for example when the gun no longer has this Mac's pairing.
	if reflect.ValueOf(dev).IsZero() {
		return nil, fmt.Errorf("airgo: connect %s: link failed during connect", info.Name)
	}
	c, err := setup(dev, info)
	if err != nil {
		_ = dev.Disconnect()
		return nil, err
	}
	return c, nil
}

func setup(dev bluetooth.Device, info Found) (*Client, error) {
	services, err := dev.DiscoverServices([]bluetooth.UUID{serviceUUID})
	if err != nil {
		return nil, fmt.Errorf("airgo: discover service: %w", err)
	}
	if len(services) != 1 {
		return nil, fmt.Errorf("airgo: service FFE0 not found on %s", info.Name)
	}
	chars, err := services[0].DiscoverCharacteristics([]bluetooth.UUID{paramsUUID, commandUUID, notifyUUID})
	if err != nil {
		return nil, fmt.Errorf("airgo: discover characteristics: %w", err)
	}
	c := &Client{
		dev:     dev,
		info:    info,
		state:   State{},
		updates: make(chan State, 1),
		acks:    make(chan CommandAck, 4),
		setErrs: make(chan SetError, 4),
		done:    make(chan struct{}),
	}
	found := 0
	for _, ch := range chars {
		switch ch.UUID() {
		case paramsUUID:
			c.params, found = ch, found|1
		case commandUUID:
			c.command, found = ch, found|2
		case notifyUUID:
			c.notify, found = ch, found|4
		}
	}
	if found != 7 {
		return nil, fmt.Errorf("airgo: characteristics incomplete on %s", info.Name)
	}

	activeMu.Lock()
	activeOnEnd = func(d bluetooth.Device) {
		if d.Address.String() == dev.Address.String() {
			c.finish()
		}
	}
	activeMu.Unlock()

	if err := c.notify.EnableNotifications(c.onNotify); err != nil {
		return nil, fmt.Errorf("airgo: subscribe: %w", err)
	}
	c.mu.Lock()
	c.lastSeen = time.Now()
	c.mu.Unlock()
	if err := c.Refresh(); err != nil {
		return nil, err
	}
	go c.watchdog()
	return c, nil
}

func (c *Client) finish() { c.doneOnce.Do(func() { close(c.done) }) }

// watchdog ends the client when the status stream stops, even if the OS has not
// noticed the link is gone.
func (c *Client) watchdog() {
	t := time.NewTicker(time.Second)
	defer t.Stop()
	for {
		select {
		case <-c.done:
			return
		case <-t.C:
			c.mu.Lock()
			quiet := time.Since(c.lastSeen)
			c.mu.Unlock()
			if quiet > linkTimeout {
				c.finish()
				return
			}
		}
	}
}

func (c *Client) onNotify(buf []byte) {
	b := append([]byte(nil), buf...)
	n, err := DecodeNotification(b)
	if err != nil {
		return // Corrupt frames carry nothing usable; the next arrives within a second.
	}
	c.mu.Lock()
	c.lastSeen = time.Now()
	c.mu.Unlock()
	switch {
	case n.Ack != nil:
		offer(c.acks, *n.Ack)
	case n.SetErr != 0:
		offer(c.setErrs, n.SetErr)
	default:
		c.merge(n.Params)
	}
}

func offer[T any](ch chan T, v T) {
	select {
	case ch <- v:
	default:
	}
}

func drain[T any](ch chan T) {
	for {
		select {
		case <-ch:
		default:
			return
		}
	}
}

func (c *Client) merge(params []Param) {
	c.mu.Lock()
	c.state.Apply(params)
	snap := c.snapshotLocked()
	c.mu.Unlock()
	// Keep only the newest state for slow consumers.
	select {
	case <-c.updates:
	default:
	}
	offer(c.updates, snap)
}

func (c *Client) snapshotLocked() State {
	return maps.Clone(c.state)
}

// Info describes the connected gun.
func (c *Client) Info() Found { return c.info }

// State returns a copy of the latest known parameters.
func (c *Client) State() State {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.snapshotLocked()
}

// Updates delivers the newest state after each status frame (about 1 Hz).
func (c *Client) Updates() <-chan State { return c.updates }

// Done is closed when the connection ends.
func (c *Client) Done() <-chan struct{} { return c.done }

// Close disconnects.
func (c *Client) Close() error {
	c.finish()
	return c.dev.Disconnect()
}

// Refresh reads all 35 parameters. Status frames only carry IDs 1-15, so this is
// how stored settings are re-read after a write.
func (c *Client) Refresh() error {
	c.opMu.Lock()
	defer c.opMu.Unlock()
	return c.refreshLocked()
}

func (c *Client) refreshLocked() error {
	buf := make([]byte, 256)
	n, err := c.params.Read(buf)
	if err != nil {
		return fmt.Errorf("airgo: read parameters: %w", err)
	}
	params, err := DecodeRead(buf[:n])
	if err != nil {
		return fmt.Errorf("airgo: read parameters (%d bytes; a link MTU under 155 truncates this): %w", n, err)
	}
	c.merge(params)
	return nil
}

// Set writes parameters and verifies them by reading back. Stored settings are
// only accepted while the gun is idle, target temperature and fan level only while
// it heats. Values are range-checked first because the firmware stores settings
// unchecked. Writing a stored setting makes the gun beep and save to flash.
func (c *Client) Set(ctx context.Context, params ...Param) error {
	f := c.State().Fahrenheit()
	for _, p := range params {
		if err := CheckValue(p.ID, p.Value, f); err != nil {
			return err
		}
	}
	frame, err := EncodeParams(params)
	if err != nil {
		return err
	}

	c.opMu.Lock()
	defer c.opMu.Unlock()
	drain(c.setErrs)
	if _, err := c.params.Write(frame); err != nil {
		return fmt.Errorf("airgo: write parameters: %w", err)
	}
	// Success sends no frame and a rejection arrives seconds later, so the state
	// decides: the firmware applies a write before answering it.
	if err := c.refreshLocked(); err != nil {
		return err
	}
	after := c.State()
	mismatch := -1
	for i, p := range params {
		if after[p.ID] != p.Value {
			mismatch = i
			break
		}
	}
	if mismatch < 0 {
		return nil
	}
	select {
	case e := <-c.setErrs:
		return e
	case <-time.After(replyTimeout):
		p := params[mismatch]
		return fmt.Errorf("airgo: parameter %d reads back %d after writing %d", p.ID, after[p.ID], p.Value)
	case <-c.done:
		return ErrLinkLost
	case <-ctx.Done():
		return ctx.Err()
	}
}

// SetTarget changes the live target temperature and fan level during heating.
// Pass 0 to leave one unchanged.
func (c *Client) SetTarget(ctx context.Context, temp, fan int16) error {
	var params []Param
	if temp != 0 {
		params = append(params, Param{ParamTargetTemp, temp})
	}
	if fan != 0 {
		params = append(params, Param{ParamFanLevel, fan})
	}
	if len(params) == 0 {
		return errors.New("airgo: nothing to set")
	}
	return c.Set(ctx, params...)
}

// send writes a command and waits for its answer. If none comes, confirmed decides
// from the gun's actual state; a nil confirmed means the effect cannot be observed.
func (c *Client) send(ctx context.Context, cmd Command, confirmed func(State) bool) error {
	c.opMu.Lock()
	defer c.opMu.Unlock()
	drain(c.acks)
	if _, err := c.command.Write(EncodeCommand(cmd)); err != nil {
		return fmt.Errorf("airgo: write command: %w", err)
	}
	timeout := time.After(replyTimeout)
	for {
		select {
		case a := <-c.acks:
			if a.Rejected {
				return ErrRejected
			}
			if a.Command == cmd {
				return nil
			}
			// A late answer to an earlier command; keep waiting for ours.
		case <-timeout:
			if err := c.refreshLocked(); err != nil {
				return err
			}
			if confirmed != nil && confirmed(c.State()) {
				return nil
			}
			return ErrNoAck
		case <-c.done:
			return ErrLinkLost
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

// StartHeating starts heating on gear 1-5, or switches gear if already heating.
//
// This turns on a 450 °C heater remotely. Only call it when someone is at the
// gun. If ParamIdleTime and ParamIdleDetect are 0 the gun has no standby timeout.
func (c *Client) StartHeating(ctx context.Context, gear int) error {
	if gear < 1 || gear > 5 {
		return fmt.Errorf("airgo: gear %d outside 1-5", gear)
	}
	return c.send(ctx, Command(gear), func(s State) bool { return s[ParamActiveGear] == int16(gear) })
}

// StopHeating stops heating. It returns ErrRejected if the gun is not heating.
func (c *Client) StopHeating(ctx context.Context) error {
	return c.send(ctx, CommandStop, func(s State) bool { return !s.Heating() })
}

// FactoryReset restores factory settings, erasing custom gears. It is only
// accepted while the gun is idle.
func (c *Client) FactoryReset(ctx context.Context) error { return c.send(ctx, CommandReset, nil) }
