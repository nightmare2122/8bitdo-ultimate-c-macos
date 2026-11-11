<!--
Draft for: https://github.com/xsyetopz/OpenJoystickDriver/issues/new/choose
Form: "Controller Support (AI Agent)". Paste each section into the field with the same name.
Before posting, replace every line marked FILL IN, read the whole draft, and tick the checkboxes yourself.
-->

# Title

[Controller]: 8BitDo Ultimate C Wired Controller for Xbox (2dc8:2027) goes dark on macOS after SET_CONFIGURATION; same handshake works on Linux

## Agent, model, and harness

claude-opus-5-5 in Claude Code 2.1.311

## Human operator

@nightmare2122

## OpenJoystickDriver version

0.6.0-alpha.1+build.1.19.64.sha.3552be925d7d (1.19.64)

## Where the build came from

GitHub release

## macOS version and build

27.2 (26B123)

## Mac model and chip

MacBook Pro (Mac15,7), Apple M3 Pro

## Controller name and maker

8BitDo Ultimate C Wired Controller for Xbox, 8BitDo

## VID:PID

2dc8:2027

## Connection

USB cable

## Mode

None. Not checked whether the controller has a mode button combination.

## System Information entry

```text
8BitDo Ultimate C Wired Controller for Xbox:

  Location ID: 0x01100000
  Connection Type: Removable
  Manufacturer: 8BitDo
  Link Speed: 12 Mb/s
  USB Vendor ID: 0x2dc8
  USB Product ID: 0x2027
  USB Product Version: 0x0114
```

Serial number left out. Values read from the IORegistry on this Mac: bcdDevice 0x0114, bDeviceClass 255, bDeviceSubClass 71, bDeviceProtocol 208, 12 Mb/s, one configuration. Interface 0: FF/47/D0, interrupt IN 0x81 and OUT 0x02, 64 bytes. Interface 1: FF/47/D0, alternate setting 1 has isochronous 0x03 OUT and 0x83 IN.

## What works

On macOS, with OpenJoystickDriver and with the operator's own libusb test scripts:

- Buttons: Not working
- Left stick: Not working
- Right stick: Not working
- Triggers: Not working
- Rumble: Not tested
- Lights: Not working. The light goes off as soon as the configuration is set.

OpenJoystickDriver lists the controller as Xbox One (GIP) over USB, and shows Input as Healthy. Not checked whether the Input Test shows any button presses. The Xbox USB Driver banner is shown, and Repair Xbox USB Driver does nothing visible. The driver extension only matches Microsoft IDs, so it should not affect this controller.

## Support report and packet capture

support-report.json from `ojd diagnose --output support-report.json`, pasted as text. It was written while the controller was not connected; a second run with the controller connected wrote a report with an empty controller list and the service shown as unavailable.

```json
{
  "data" : {
    "apiVersion" : "openjoystickdriver\/v1alpha1",
    "applicationService" : {
      "activeCount" : 1,
      "connected" : true,
      "installed" : true,
      "runtimeState" : "running"
    },
    "configuration" : {
      "userSpaceVirtualDeviceEnabled" : true
    },
    "controllers" : [

    ],
    "frameTimings" : [

    ],
    "hidGamepads" : [

    ],
    "kind" : "SupportDiagnostic",
    "notes" : [
      "Review this file before sharing. Device product names are included.",
      "Serial values, paths, packet payloads, HID locations, and discovery text are excluded."
    ],
    "permissions" : {
      "inputInject" : "Granted",
      "inputRead" : "Granted"
    },
    "privacy" : {
      "includesDeviceProductNames" : true,
      "includesFilesystemPaths" : false,
      "includesHIDLocationIDs" : false,
      "includesPacketPayloads" : false,
      "includesRawSerialNumbers" : false
    },
    "system" : {
      "architecture" : "arm64",
      "buildIdentity" : {
        "appBundleVersion" : "1.19.64",
        "semanticVersion" : "0.6.0-alpha.1",
        "sourceCommit" : "3552be925d7dacef3fd895ac0c6cbf39e9d3cd70",
        "sourceState" : "Clean"
      },
      "osName" : "macOS",
      "osVersion" : "27.2.0"
    },
    "systemGamepadAudit" : {
      "bundleVersions" : [
        "18.6.1"
      ],
      "catalogListedOJDRecordCount" : 0,
      "caveat" : "Private current-system catalog observation only. A missing entry does not prove GameController incompatibility, and a listed entry does not replace a live GCController.supportsHIDDevice or hardware test.",
      "ojdRecordCount" : 0,
      "records" : [

      ],
      "skippedSystemDeviceCount" : 0,
      "source" : "PreinstalledMobileAsset",
      "systemExactDeviceCount" : 185,
      "warnings" : [

      ]
    },
    "unboundDevices" : [

    ]
  },
  "datacontenttype" : "application\/json",
  "dataschema" : "https:\/\/xsyetopz.github.io\/OpenJoystickDriver\/schemas\/v1alpha1\/report.schema.json#\/$defs\/supportDiagnosticData",
  "id" : "A825B51F-FAF2-497C-87A6-8AAE8B5814CC",
  "source" : "urn:openjoystickdriver:support",
  "specversion" : "1.0",
  "time" : "2026-11-03T14:30:42Z",
  "type" : "com.openjoystickdriver.support.diagnostic"
}
```

With the controller connected, `ojd controller list` printed:

```text
E-UzQJeTJTE-IlwG82  U-B4Xfj8Ci_8XLLKRP  2DC8:2027  USB  8BitDo Ultimate C Wired Controller for Xbox
```

and `ojd diagnose` printed `Vendor-specific USB controllers visible: 1` and `Frames in the window: 0`.

The OpenJoystickDriver log for each connection:

```text
[DeviceManager] USB device added: 8BitDo Ultimate C Wired Controller for Xbox (LogicalControllerID(VID:0x2DC8 PID:0x2027 serial=present loc=17825792 if=0))
[DevicePipeline] Handshake complete: LogicalControllerID(VID:0x2DC8 PID:0x2027 serial=present loc=17825792 if=0)
[UserSpaceOutputDispatcher] Created virtual device for LogicalControllerID(VID:0x2DC8 PID:0x2027 serial=present loc=17825792 if=0)
[DevicePipeline] Starting USB input loop: LogicalControllerID(VID:0x2DC8 PID:0x2027 serial=present loc=17825792 if=0) inEP=0x81
[DevicePipeline] Device disconnected: LogicalControllerID(VID:0x2DC8 PID:0x2027 serial=present loc=17825792 if=0)
```

Not attached: an `ojd controller capture`, because the controller sends no input on macOS. Not attached: the Linux usbmon capture, because the form takes no files here; the operator can add it.

## Other details

The same controller works on Linux. Fedora, kernel 7.2.11-100.fc43.x86_64, xpad driver. On Linux the light goes solid, and the controller sends 36-byte input reports, status reports (`03 20 ..`), and guide reports (`07 20 ..`).

What Linux sends, from the usbmon capture:

1. SET_CONFIGURATION 1
2. SET_INTERFACE interface 1, alternate setting 0
3. `05 20 00 01 00`, then `0a 20 01 03 00 01 14`, then `06 20 02 02 01 00` on endpoint 0x02

The controller replies within 1 ms and input starts.

On Linux, with xpad blocked, the operator's own libusb script also worked in every case: with or without SET_INTERFACE, with the packets sent 1 s or 10 s late, and with SET_CONFIGURATION sent twice. With only SET_CONFIGURATION and nothing else, the light stays solid on Linux.

On macOS, the same libusb script with byte-identical packets gets no data on endpoint 0x81. Every write completes. What the operator saw on macOS:

- The light goes off when the configuration is set. This happens with only SET_CONFIGURATION and no other request.
- The controller does not answer any packet. A GIP identify request (`04 20 01 00`) got no reply either.
- About 10 to 25 s after the GIP packets are sent, the kernel logs `AppleUSBHostPort::terminateDevice ... hardware connection lost` and the controller enumerates again. With only SET_CONFIGURATION, it stays connected for at least 35 s.
- A raw SET_INTERFACE control request is logged as `intercepting manually crafted SET_INTERFACE request`. Setting the alternate setting through the interface API did not change the result.
- SET_CONFIGURATION 0 and then 1, and CLEAR_FEATURE remote wakeup, did not change the result. GET_STATUS reads `02 00` both before and after the CLEAR_FEATURE.
- Sending the packets within 32 ms of SET_CONFIGURATION, and using a read that never times out, did not change the result.
- Quitting Chrome and Spotify, which both open the device when it enumerates, did not change the result.
- Not checked: whether macOS sends SET_FEATURE remote wakeup, or other requests, during enumeration or configuration. USB capture is not available on this Mac without turning off SIP.

## Tested change

No change

## How the operator tested the change

No tested change

## Prompt for the maintainer's agent

```text
No tested change
```

## Operator confirmation

- [ ] The human operator read this whole issue before submitting it.
- [ ] Every value and output in this issue comes from a command that ran on the operator's Mac with this controller connected.

## Before you submit

- [ ] I searched existing issues and found no report of this.
- [ ] I read Supported controllers.
- [ ] This issue covers one controller.
