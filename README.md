# 8BitDo Ultimate C on macOS

**Problem:** the *8BitDo Ultimate C Wired Controller for Xbox* (USB `2dc8:2027`) works on Linux with the
`xpad` driver, but goes dark on macOS: it enumerates, then stops sending input after `SET_CONFIGURATION`.
No macOS driver I tried (including OpenJoystickDriver and several Xbox/GIP drivers) revives it.

**This repo** collects the investigation and two working workarounds that route the controller through a
Linux machine.

| Folder | What it is | Status |
|---|---|---|
| [`padbridge/`](padbridge) | **PadBridge**: a Linux laptop poses over Bluetooth as a native Xbox One S or DualShock 4 controller; the Mac sees a real gamepad (Game Controller framework, Steam, games). Linux host service + GTK app, and a macOS menu-bar receiver app (Swift). | Working |
| [`ds4-bt-bridge/`](ds4-bt-bridge) | The Bluetooth engine PadBridge runs (evdev in, HID over L2CAP out, rumble forwarded back), plus setup script and latency tools. | Working |
| [`bridge/`](bridge) | Simpler fallback: read the controller on Linux and send it over UDP to the Mac, where it becomes keyboard and mouse input. Python stdlib only. | Working, keyboard/mouse only |
| [`research/`](research) | Probes, logs, USB captures and OpenJoystickDriver records from diagnosing the macOS failure. | Reference |
| [`docs/`](docs) | PadBridge contract between host, receiver and engine; draft bug report for OpenJoystickDriver. | Reference |

## How it works

```
controller --USB--> Linux laptop (xpad + engine) --Bluetooth Classic HID--> Mac (sees "Xbox Wireless Controller")
```

macOS cannot create a virtual gamepad without an Apple-granted entitlement, so instead of faking one on the Mac,
the Linux laptop impersonates a real Bluetooth controller. See [`docs/PADBRIDGE_CONTRACT.md`](docs/PADBRIDGE_CONTRACT.md)
for the protocol, status file and command line, and [`bridge/README.md`](bridge/README.md) for the UDP fallback.

## Requirements

- Mac: macOS 26+ on Apple Silicon, Xcode / Swift toolchain to build the receiver.
- Linux host: Fedora 43 with GNOME and BlueZ (tested); a Bluetooth adapter; the controller on USB.
- While PadBridge is installed, the laptop's own Bluetooth keyboards, mice and headphones stop working
  (the engine needs the HID channels). Uninstalling restores them.

## Quick start

1. **Linux:** `cd padbridge/host && sudo ./install.sh`, then log out and in once. See [`padbridge/host/README.md`](padbridge/host/README.md).
2. **Mac:** `cd padbridge/receiver && ./build.sh`, open the built `PadBridge.app`, and pair when prompted.
3. Run the host tests: `python3 -m unittest discover -s padbridge/host/tests`.

## Research notes

- `research/captures/` holds a scrubbed usbmon capture of the working Linux enumeration (serial removed) to compare against macOS.
- `research/probes/` has IOUSBHost / IOKit probes and Linux capture scripts.
- `research/ojd_records/` has OpenJoystickDriver device records and handshake variants.
- Raw Linux dmesg bundles were left out on purpose because they identify the machine.

## Caveats

- Personal machine details (hostnames, IPs, Bluetooth addresses, SSH key names) are replaced with placeholders such as `USER@LAPTOP_IP`.
- Unofficial, unaffiliated with 8BitDo, Microsoft or Sony. Use at your own risk; the Linux setup rewrites Bluetooth configuration (backed up as `main.conf.ds4bak`).
- Related projects consulted but not vendored: [OpenJoystickDriver](https://github.com/xsyetopz/OpenJoystickDriver),
  [X360MacOSReceiverBridge](https://github.com/jonasw8/X360MacOSReceiverBridge),
  [razer-wolverine-macos](https://github.com/davefmurray/razer-wolverine-macos),
  [xinput-controller](https://github.com/meghrathod/xinput-controller),
  [gipbridge](https://github.com/ysSemanticSystems/gipbridge).

## License

MIT, see [LICENSE](LICENSE).
