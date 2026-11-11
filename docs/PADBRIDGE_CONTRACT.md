# PadBridge: shared contract for the host app, the receiver app and the engine

PadBridge turns a Linux laptop into a Bluetooth game controller for a Mac. A wired controller (here an
8BitDo Ultimate C for Xbox, which macOS cannot drive over USB) plugs into the Linux laptop; the laptop
poses over Bluetooth Classic as an **Xbox Wireless Controller** (Xbox One S, `045e:02fd`) or a **DualShock 4**
(`054c:09cc`). macOS treats it as a native controller. This works today; this document is what the three
work streams build against.

## Machines

| Machine | Access | Notes |
|---|---|---|
| Mac (receiver) | local shell | macOS 27.2, Apple Silicon, Xcode 27.2, Swift 6.4. `blueutil` installed (Homebrew). |
| Fedora 43 laptop (host) | `ssh -i ~/.ssh/id_ed25519_laptop -o IdentitiesOnly=yes -o BatchMode=yes USER@LAPTOP_IP` | GNOME, Python 3.14, BlueZ 5.87, kernel 7.2. The controller is on USB there (xpad). |

Passwordless sudo on the laptop is limited to exactly: `/usr/bin/btmon, /usr/bin/btmgmt, /usr/bin/bluetoothctl,
/usr/bin/systemctl, /usr/bin/journalctl, /usr/bin/pkill, /usr/bin/python3 /home/USER/ds4-bt-bridge/ds4_bt_bridge.py *,
/usr/bin/bash /home/USER/ds4-bt-bridge/ds4_bt_setup.sh *`. Use `sudo -n`. Anything else needs the user's
password: write it into an install script and ask the coordinator, do not try to work around it.

SSH gotcha: never put the string `ds4_bt_bridge.py` in a remote command line together with a `pkill -f`
(the pattern matches your own shell and kills the SSH session). Use `~/ds4-bt-bridge/ctl.sh`
(`start [args] | stop | status | log | trace [n] | sniff [secs] | sniffed`).

## The engine (exists, working)

Source of truth on the Mac: `<repo>/ds4-bt-bridge/`; deployed copy on the laptop:
`~/ds4-bt-bridge/` (copy with `scp`). Files:

- `ds4_bt_bridge.py`: the engine (runs as root). Reads the controller via evdev, publishes a HID SDP record,
  owns L2CAP PSM 17/19, pairs with the Mac (Just Works), streams input reports, forwards rumble.
- `bridge_linux.py`: evdev reading helpers (imported by the engine).
- `ds4_bt_setup.sh`: one-time system setup, root. `xbox-one` (default; `xbox` = old name) | `ds4` | `--undo`
  (`xbox-series` is paused and refused). Restarts bluetoothd with
  `--noplugin=input,hostname,a2dp,avrcp`, writes Name/Class/DeviceID/JustWorksRepairing into
  `/etc/bluetooth/main.conf` (backup `main.conf.ds4bak`), disables WirePlumber Bluetooth audio for the user.
- `ctl.sh`: developer helper on the laptop (see above).
- `ds4_bt_host.txt` (runtime, next to the engine): the paired Mac's address; the engine connects out to it.
- `ds4_bt_keys.json` (runtime, root 0600): link keys the engine saw at pairing. A programmatic pair from the Mac
  (`blueutil --pair`) is "No Bonding", so BlueZ forgets it on disconnect while the Mac keeps it; at startup the engine
  restores a lost pairing into BlueZ and restarts bluetoothd once (log: "BlueZ lost the pairing ... restoring it").
- `paused/`: the Xbox Series (Bluetooth LE) engine and setup snapshot. Not deployed, not run (see Addendum A).

### Engine command line

```
python3 ds4_bt_bridge.py [-v] [--mode xbox-one|xbox|ds4] [--name NAME] [--rate 250] [--adapter hci0] [--no-grab]
                         [--forget] [--status-file PATH] [--usb-sniff SECS] [--rumble-test] [--inflight 2] [--master]
```
`--inflight N` (added by the latency stream): at most N of our reports inside the Bluetooth radio at once, counted
exactly from a raw HCI socket (ACL frames to the controller vs Number_Of_Completed_Packets). Default 2; 0 = old
behaviour (gate on the kernel socket queue only, the radio then buffers up to 6 stale reports).
`--master` (experimental, default off): keep the master role on new links. Measured: no latency benefit.
`--mode` defaults to whatever `ds4_bt_setup.sh` applied (marker `#PadBridgeIdentity = <id>` in main.conf, else
DeviceID). `xbox` = `xbox-one`. `--mode xbox-series` exits with "xbox-series ... is paused; use xbox-one or ds4."
`--name NAME` (identity stream): Bluetooth name to give the Mac instead of the detected controller's short name. Exit = stop. It reconnects
to the Mac by itself and survives the controller being unplugged and replugged.

### Status file (`--status-file PATH`), rewritten atomically every 0.5 s, JSON, `version: 1`

```json
{
  "version": 1, "pid": 1234, "started_at": 1762834000.1, "updated_at": 1762834100.6,
  "mode": "xbox", "name": "Xbox Wireless Controller", "adapter": "AA:BB:CC:DD:EE:02",
  "controller": {"connected": true, "path": "/dev/input/event19"},
  "host": {"address": "AA:BB:CC:DD:EE:01", "connected": true, "since": 1762834010.0},
  "link": {"handle": 3, "sniff_disabled": true},
  "stats": {"reports_per_s": 229.0, "max_queue_bytes": 960, "deferred": 40, "input_age_ms": 1.2, "pings": 0},
  "rumble": {"strong": 0, "weak": 0},
  "input": {"lx": 0, "ly": 0, "rx": 0, "ry": 0, "lt": 0, "rt": 0, "buttons": ["A", "Up"]},
  "log": ["05:24:39 Mac AA:BB:CC:DD:EE:01 connected", "..."]
}
```
Sticks are -32767..32767 (y down positive), triggers 0..65535. Button names: A B X Y LB RB View Menu Guide LS RS
Up Down Left Right Share. `input` is refreshed on every write; `stats` cover a rolling 0.5 s window.
`mode` keeps its old values ("xbox" for xbox-one, "ds4"); `name` is the emulated identity's standard name; the name
the Mac is actually given is `bt_name`. Identity keys (`identity`, `identities`, `bt_name`, extended `controller`): see
Addendum A, implemented.
Added stats keys (latency stream): `input_age_max_ms` (worst host-side age in the window), `link_ms` / `link_p99_ms`
(radio delivery: report handed to the Bluetooth controller -> controller confirms delivery), `max_inflight`
(most of our reports inside the radio at once); all null when not measurable. `input_age_ms`: mean time from the kernel's controller event timestamp to the report
being handed to Bluetooth (host-side processing latency). The latency work stream may add keys; it must not
remove or rename these.

### Latency ping (Xbox mode only), for the receiver app

Mac → output report 3 (8 bytes): `00 00 00 00 00 A5 <seq> 5A` (enable 0 = no rumble, duration 0xA5, start delay
= seq 0-255, loop 0x5A). The engine immediately answers with input report 4 (1 byte): `<seq>`. On the Mac:
`IOHIDDeviceSetReport(dev, kIOHIDReportTypeOutput, 3, ...)` on the device matching VendorID 0x045E ProductID
0x02FD (match on VID/PID, not on the product name: the name is now the physical controller's, e.g. "8BitDo Ultimate C"), and `IOHIDDeviceRegisterInputReportCallback` to catch report 4. Round trip = Mac → Bluetooth → laptop →
Bluetooth → Mac. It does not include the controller's own USB hop into the laptop (see `input_age_ms`).
Rumble (real): output report 3 = `enable(0x01 weak,0x02 strong) lt rt strong weak duration delay loop`,
magnitudes 0-100, duration/delay in 10 ms. DS4 mode: output report 0x11 (byte 6 weak, 7 strong).

## Work streams and file ownership

| Stream | Owns (may write) | Must not touch |
|---|---|---|
| Latency | `ds4-bt-bridge/*` (engine) on Mac and laptop; the laptop's Bluetooth while running | `padbridge/host/`, `padbridge/receiver/` |
| Host app (Linux) | `padbridge/host/` | the engine files (vendor a copy into the package at build time) |
| Receiver app (Mac) | `padbridge/receiver/` | engine files, laptop |

Hardware: only one engine can run on the laptop at a time (it owns the Bluetooth HID channels). The latency
stream has the laptop until the coordinator says otherwise; the host-app stream develops against a mock status
file and must not start/stop the engine or bluetoothd until told. The receiver stream may use the Mac's live
controller connection read-only plus short ping/rumble tests.

Root path for the product: `<repo>/padbridge/`.

## Addendum A (coordinator, 11 Nov): identities and real-controller detection (engine work scheduled after the latency stream)

Design against these now; the engine will implement them next. Until then, mock them.

**Identities** (what macOS sees). Ids, labels, Bluetooth transport, and the IDs macOS matches on:

| id | label | transport | VID:PID |
|---|---|---|---|
| `xbox-series` | Xbox Series X\|S Controller | Bluetooth LE (HID over GATT) | 045E:0B13 |
| `xbox-one` | Xbox Wireless Controller (Xbox One S) | Bluetooth Classic | 045E:02FD |
| `ds4` | DualShock 4 | Bluetooth Classic | 054C:09CC |

`ds4_bt_setup.sh` will accept these ids (old `xbox` = `xbox-one`). The engine's `--mode` accepts them too. Switching identity
requires the Mac to forget and re-pair the device (the host app should guide that; the receiver app should detect a
stale/mismatched device). LE may have higher latency than Classic (connection interval); the apps should show that
trade-off in the picker, e.g. "Lowest latency" on the Classic options, once measured.

**Status JSON additions** (status file `version` stays 1; new keys only):
```json
"controller": {"connected": true, "path": "/dev/input/event19", "name": "8BitDo Ultimate C Wired Controller for Xbox",
               "vendor": "8BitDo", "vid": "2dc8", "pid": "2027", "bus": "usb"},
"identity":   {"id": "xbox-one", "label": "Xbox Wireless Controller (Xbox One S)", "transport": "classic",
               "vid": "045e", "pid": "02fd"},
"identities": [{"id": "xbox-series", "label": "...", "transport": "le", "vid": "045e", "pid": "0b13"}, ...],
"bt_name": "8BitDo Ultimate C"
```

**Real controller on the Mac**: the host's Bluetooth device name will be the physical controller's short name (e.g.
"8BitDo Ultimate C"), so macOS and `GCController.vendorName` show it. The receiver app should show both the physical
controller (from the device name) and the emulated identity (from VID:PID / productCategory), e.g. "8BitDo Ultimate C,
appearing as Xbox Series X|S Controller". macOS may cache an old name until the device is forgotten and re-paired.
The latency ping works in both Xbox identities (same report 3 / report 4 contract); not in `ds4`.

### Addendum A status (identity stream, 11 Nov): implemented except xbox-series (paused)

| id | status | macOS shows (after re-pair) |
|---|---|---|
| `xbox-one` | implemented, verified on hardware: pair, rumble, ping (RTT p50 9.7 / p90 34.9 / p99 64.9 ms), input->air p50 8.0 / p99 64.6 ms; the user played with it (stick input in the engine log); a 45 s GCController watch saw no input because the user was idle | device "8BitDo Ultimate C"; HID Product and `GCController.vendorName` "8BitDo Ultimate C"; productCategory "Xbox One"; VID:PID 045E:02FD |
| `ds4` | implemented, verified on hardware: pair, input through GCController (A, X, Up, Down, right stick -0.94..1.00 / -1.00..0.63 from the user's play), rumble on and off; no ping (radio delivery about 8 ms, like xbox-one) | Bluetooth list "8BitDo Ultimate C", but HID Product / `vendorName` "DUALSHOCK 4 Wireless Controller" (Apple's Sony driver names it itself); productCategory "DualShock 4"; 054C:09CC |
| `xbox-series` | **paused** (user decision). Not in `identities[]`; `--mode xbox-series` and `ds4_bt_setup.sh xbox-series` refuse with "paused". Code kept in `ds4-bt-bridge/paused/`. | n/a |

- `identities[]` lists only `xbox-one` and `ds4` while xbox-series is paused. Apps should build the picker from it.
- `controller`: `connected, path, name, vendor, vid, pid, bus` as specified, plus `phys, uniq, usb_product,
  evdev_name`. `name` is the USB product string when xpad gives a generic name ("Generic X-Box pad"). The device is any
  joystick node with BTN_GAMEPAD that is not virtual, preferring USB. A replug or a different controller updates it.
  On unplug `connected` goes false and the last name, vendor and IDs are kept.
- `bt_name`: a short name from a known-device table (vid:pid), else the USB product string with "Wired/Wireless
  Controller for Xbox", "Controller", "Gamepad" and similar removed, prefixed by the vendor. Max 29 characters.
  It is set as `Adapter1.Alias` and updated live when the controller changes.
- **Where macOS gets the name:** the Bluetooth remote name (Alias), read when it pairs. It does not use the SDP
  ServiceName, which is still "Xbox Wireless Controller". A plain reconnect keeps the cached name.
- **Name refresh / identity switch (verified):** on the laptop `ctl.sh stop`; `sudo bash ds4_bt_setup.sh <id>` (only
  when switching identity); `ctl.sh start ...`. On the Mac: `blueutil --unpair 9c-2f-9d-a1-96-14`, then `blueutil --pair
  9c-2f-9d-a1-96-14` (it returns once paired, or kill it after about 20 s; the Mac connects anyway). If blueutil fails,
  use System Settings > Bluetooth > (i) > Forget This Device, then Connect.
  `blueutil --pair` pairs with "No Bonding". The engine handles that (ds4_bt_keys.json above), so the pairing
  survives bluetoothd restarts and reboots.
- **Name stickiness after `ds4`:** macOS's DualShock driver renames the device record to "DUALSHOCK 4 Wireless
  Controller". After switching back to xbox-one, that name stays: in blueutil, the HID Product and `vendorName`,
  through blueutil unpair/pair and reconnect. productCategory is still correct ("Xbox One"). Only System Settings >
  Bluetooth > Forget This Device (or Rename) clears it. Apps should take the physical controller from the status
  file's `bt_name`/`controller`, not from the Mac-side name.
- macOS `system_profiler` may keep stale Vendor/Product IDs for the address (it showed 1D6B:0246 left over from the
  LE test). The HID device and GameController use the right IDs. Match on the IOHID device, not on system_profiler.

**xbox-series (Bluetooth LE) findings before the pause**, for later:
- It is feasible on this adapter (Realtek, BlueZ 5.87). The engine registered a GattManager1 application: HID
  0x1812 with Report Map (xpadneo's 283-byte firmware 5.x descriptor plus input report 4 for the ping), Reports 1 in,
  4 in, 3 out with Report Reference, HID Information, Protocol Mode, Control Point; DIS 0x180A with PnP ID 02/045E/0B13/0517;
  Battery 0x180F. It advertised through LEAdvertisingManager1 (UUID 0x1812, appearance 0x03C4, name). The adapter
  was LE-only (`ControllerMode = le`).
- macOS found it (CoreBluetooth scan: name, HID UUID, appearance 964). A CoreBluetooth `connect()` from a tool was
  enough for macOS's own HID-over-GATT host to take over. It bonded (LE Secure Connections, Just Works, "Bonding, No
  MITM, SC"), negotiated ATT MTU 517, and subscribed to input reports 1 and 4 and battery. The engine streamed through
  AcquireNotify sockets at 117 reports/s. Radio delivery (handed to the controller until completed) averaged 16.4 ms
  (p99 22 ms), which suggests about a 15 ms connection interval. The granted interval was not confirmed with btmon.
- Blocker 1 (fixed in paused code): BlueZ serves its own Device Information service (default DeviceID 1D6B:0246),
  listed before ours. macOS took VID:PID 1D6B:0246 from it, so there was no GCController. Fix: `DeviceID = false` in
  main.conf. After that only the engine's DIS (045E:0B13) remained.
- Blocker 2 (open): `blueutil --unpair` cannot remove an LE bond (macOS keeps it listed as paired). The stale bond
  made macOS drop each new connection (remote user terminated). Re-pairing needs System Settings > Forget This
  Device, which could not be done while the user was in a full-screen game. So it is unverified whether macOS
  matches 045E:0B13 over LE as productCategory "Xbox Series", and latency and the ping were not measured.
- Next steps: Forget the device in System Settings, run `paused/ds4_bt_setup_le.sh xbox-series`, run the paused engine
  (copy it next to bridge_linux.py), and connect with a CoreBluetooth `connect()`. Then check `GCController`
  productCategory and measure with btmon. `[LE] MinConnectionInterval=6 / MaxConnectionInterval=9` makes the kernel
  ask for 7.5-11.25 ms. `tools/btping` matches 045E:02FD and would need 045E:0B13.
