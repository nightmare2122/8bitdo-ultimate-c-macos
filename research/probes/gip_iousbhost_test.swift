// GIP test for the 8BitDo Ultimate C (2dc8:2027) through IOUSBHost, with two macOS-specific knobs:
//   --capture   open the device with IOUSBHostObjectInitOptionsDeviceCapture (needs sudo)
//   --idle N    set the interface and pipe idle-suspend timeout to N seconds (default: leave as is)
// Waits for the controller, configures it, sends the Linux xpad startup, and prints every IN packet for 20 s.
// Build: swiftc -O gip_iousbhost_test.swift -o gip_iousbhost_test
import Foundation
import IOKit
import IOUSBHost

let vid = 0x2DC8, pid = 0x2027
let args = CommandLine.arguments
let capture = args.contains("--capture")
let idle: TimeInterval? = args.firstIndex(of: "--idle").flatMap { Double(args[$0 + 1]) }
let t0 = Date()
func log(_ s: String) { print(String(format: "%7.3f ", Date().timeIntervalSince(t0)) + s); fflush(stdout) }
func hex(_ d: Data) -> String { d.map { String(format: "%02x", $0) }.joined(separator: " ") }

func firstService(_ cls: String, _ pred: (io_service_t) -> Bool) -> io_service_t? {
  var it: io_iterator_t = 0
  guard IOServiceGetMatchingServices(kIOMainPortDefault, IOServiceMatching(cls), &it) == KERN_SUCCESS else { return nil }
  defer { IOObjectRelease(it) }
  while case let s = IOIteratorNext(it), s != 0 {
    if pred(s) { return s }
    IOObjectRelease(s)
  }
  return nil
}
func prop(_ s: io_service_t, _ k: String) -> Int? {
  IORegistryEntryCreateCFProperty(s, k as CFString, kCFAllocatorDefault, 0)?.takeRetainedValue() as? Int
}

log("capture=\(capture) idle=\(idle.map { "\($0)" } ?? "unchanged"); waiting for 2dc8:2027 ...")
var dev: io_service_t = 0
while true {
  if let s = firstService("IOUSBHostDevice", { prop($0, "idVendor") == vid && prop($0, "idProduct") == pid }) { dev = s; break }
  usleep(100_000)
}
log("found; kUSBCurrentConfiguration=\(prop(dev, "kUSBCurrentConfiguration").map(String.init) ?? "none")")

let q = DispatchQueue(label: "gip")
let device: IOUSBHostDevice
do {
  device = try IOUSBHostDevice(__ioService: dev, options: capture ? [.deviceCapture] : [], queue: q,
                               interestHandler: { _, type, _ in log(String(format: "device interest 0x%08x", type)) })
} catch { log("open device failed: \(error)"); exit(1) }
log("device opened")

do {
  try device.__configure(withValue: 1, matchInterfaces: true)
  log("configured 1")
} catch { log("configure failed: \(error)") }

var intfService: io_service_t = 0
for _ in 0..<50 {
  if let s = firstService("IOUSBHostInterface", {
    prop($0, "idVendor") == vid && prop($0, "idProduct") == pid && prop($0, "bInterfaceNumber") == 0
  }) { intfService = s; break }
  usleep(100_000)
}
guard intfService != 0 else { log("interface 0 never appeared"); exit(1) }
let intf: IOUSBHostInterface
do {
  intf = try IOUSBHostInterface(__ioService: intfService, options: [], queue: q,
                                interestHandler: { _, type, _ in log(String(format: "interface interest 0x%08x", type)) })
} catch { log("open interface failed: \(error)"); exit(1) }
log("interface 0 opened; idleTimeout=\(intf.idleTimeout)")

let inPipe = try! intf.copyPipe(withAddress: 0x81)
let outPipe = try! intf.copyPipe(withAddress: 0x02)
if let idle {
  do {
    try intf.setIdleTimeout(idle)
    try inPipe.setIdleTimeout(idle)
    try outPipe.setIdleTimeout(idle)
    log("idle timeouts set: interface=\(intf.idleTimeout) in=\(inPipe.idleTimeout) out=\(outPipe.idleTimeout)")
  } catch { log("setIdleTimeout failed: \(error)") }
}

var received = 0
func read() {
  guard let buf = try? intf.ioData(withCapacity: 64) else { return }
  do {
    try inPipe.enqueueIORequest(with: buf, completionTimeout: 0) { status, count in
      if status == kIOReturnSuccess {
        received += 1
        if received <= 30 { log("<- " + hex(Data(bytes: buf.bytes, count: count))) }
        read()
      } else {
        log(String(format: "IN read ended: 0x%08x", status))
      }
    }
  } catch { log("enqueue IN failed: \(error)") }
}
q.sync { read() }

for (i, pkt) in [[0x05, 0x20, 0, 0x01, 0x00], [0x0A, 0x20, 1, 0x03, 0x00, 0x01, 0x14], [0x06, 0x20, 2, 0x02, 0x01, 0x00]].enumerated() {
  do {
    let buf = try intf.ioData(withCapacity: pkt.count)
    buf.replaceBytes(in: NSRange(location: 0, length: pkt.count), withBytes: pkt.map(UInt8.init))
    var sent = 0
    try outPipe.__sendIORequest(with: buf, bytesTransferred: &sent, completionTimeout: 0)
    log("-> " + hex(Data(pkt.map(UInt8.init))) + " (packet \(i), \(sent) bytes)")
  } catch { log("send failed: \(error)") }
}

DispatchQueue.main.asyncAfter(deadline: .now() + 20) {
  log("done: \(received) IN packets")
  try? inPipe.__abort(with: .synchronous)
  intf.destroy()
  device.destroy()
  exit(0)
}
RunLoop.main.run()
