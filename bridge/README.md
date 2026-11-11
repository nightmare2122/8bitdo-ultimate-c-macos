# 8BitDo Ultimate C bridge (Fedora PC → Mac)

The controller works on Linux (xpad) but goes dark on macOS. The bridge reads it on the Fedora PC and sends its state to the Mac, where it becomes keyboard and mouse input.

```
controller ──USB──▶ Fedora (xpad + bridge_linux.py) ──UDP 47800──▶ Mac (bridge_mac.py) ──▶ keys / mouse
```

Both scripts use only the Python standard library.

## Connect the two machines

Either option works:

- **Same network:** Wi-Fi or LAN. Use the Mac's IP address. `bridge_mac.py` prints it when it starts.
- **Ethernet cable straight between them:** with DHCP, both machines give themselves a 169.254.x.x address after about 30 s. Use the Mac's 169.254 address, or use `--broadcast` on Fedora. For the lowest latency, use the cable.

## On the Mac

```bash
python3 bridge_mac.py --monitor
```

This only displays what arrives, so use it to check the link first. Then run the bridge for real:

```bash
python3 bridge_mac.py
```

The first time, give your terminal app Accessibility permission in System Settings › Privacy & Security › Accessibility, then restart the script. Without that permission, macOS drops the input without telling you. `--dry-run` prints the keys and clicks the bridge would send, without sending them.

## On Fedora

Copy `bridge_linux.py` to the PC, plug in the controller, then run:

```bash
sudo python3 bridge_linux.py 192.168.0.5
```

Replace the address with the Mac's. Add `-v` to print every change. By default, the script takes exclusive use of the controller, so the Linux desktop ignores it. Use `--no-grab` to share it with the desktop. If you unplug the controller and plug it back in, the script picks it up again.

## Layout

Edit `bridge_mapping.json`. Default layout:

- **Left stick:** WASD.
- **Right stick:** mouse.
- **RT / LT:** left / right click.
- **A:** space.
- **B:** escape.
- **LS click:** shift.
- **D-pad:** arrow keys.

You can map each stick to `wasd`, `arrows`, `mouse`, `scroll` or `none`.

## Limits

- **Keyboard and mouse only.** Apps that read a gamepad directly (Game Controller framework, Steam Input) do not see a controller. To create a virtual gamepad, macOS requires an entitlement that only Apple grants, and an unsigned process gets `NULL` from `IOHIDUserDeviceCreateWithProperties`.
- **No rumble and no lights.** Input only goes one way, from the PC to the Mac.
- **If the link drops for 0.5 s,** the Mac releases every held key and button.
