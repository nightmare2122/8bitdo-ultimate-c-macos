// btping: measure the PadBridge round trip Mac -> Bluetooth -> laptop engine -> Bluetooth -> Mac.
//
//   swiftc -O btping.swift -o btping
//   ./btping [count=1000] [gap_ms=8] [csv_out]
//
// Contract (PADBRIDGE_CONTRACT.md, "Latency ping"): output report 3 = 00 00 00 00 00 A5 <seq> 5A; the engine
// answers at once with input report 4 = <seq>. The receive time is the kernel's arrival timestamp from
// IOHIDDeviceRegisterInputReportWithTimeStampCallback, so run-loop scheduling on the Mac does not inflate it.
// Also counts input report 1 (the controller stream) to show the Mac-side arrival rate and gaps.
import Foundation
import IOKit.hid

let args = CommandLine.arguments
let total = args.count > 1 ? Int(args[1]) ?? 1000 : 1000
let gapMs = args.count > 2 ? Double(args[2]) ?? 8 : 8
let csvPath = args.count > 3 ? args[3] : nil

var tb = mach_timebase_info_data_t()
mach_timebase_info(&tb)
func ns(_ t: UInt64) -> Double { Double(t) * Double(tb.numer) / Double(tb.denom) }

let mgr = IOHIDManagerCreate(kCFAllocatorDefault, IOOptionBits(kIOHIDOptionsTypeNone))
IOHIDManagerSetDeviceMatching(mgr, [kIOHIDVendorIDKey: 0x045E, kIOHIDProductIDKey: 0x02FD] as CFDictionary)
IOHIDManagerOpen(mgr, IOOptionBits(kIOHIDOptionsTypeNone))
guard let dev = (IOHIDManagerCopyDevices(mgr) as? Set<IOHIDDevice>)?.first else {
    print("no Xbox (045E:02FD) HID device; is the bridge connected?"); exit(1)
}

var sentAt = [UInt8: UInt64]()      // seq -> mach time just before SetReport
var rtts = [Double]()               // ms, kernel arrival - send
var setReportMs = [Double]()        // how long the SetReport call itself blocked
var lost = 0, seq: UInt8 = 0, outstanding = false, lastSend: UInt64 = 0
var r1Count = 0, r1Last: UInt64 = 0, r1Gaps = [Double]()
let reportBuf = UnsafeMutablePointer<UInt8>.allocate(capacity: 64)

let cb: IOHIDReportWithTimeStampCallback = { _, _, _, _, reportID, report, length, timeStamp in
    if reportID == 1 {
        if r1Last != 0 { r1Gaps.append((ns(timeStamp) - ns(r1Last)) / 1e6) }
        r1Last = timeStamp; r1Count += 1
        return
    }
    guard reportID == 4, length >= 1 else { return }
    let s = length >= 2 && report[0] == 4 ? report[1] : report[0]
    if let t0 = sentAt.removeValue(forKey: s) {
        rtts.append((ns(timeStamp) - ns(t0)) / 1e6)
        outstanding = false
    }
}
IOHIDDeviceRegisterInputReportWithTimeStampCallback(dev, reportBuf, 64, cb, nil)
IOHIDDeviceScheduleWithRunLoop(dev, CFRunLoopGetCurrent(), CFRunLoopMode.defaultMode.rawValue)

func sendPing() {
    seq &+= 1
    let r: [UInt8] = [0x03, 0, 0, 0, 0, 0, 0xA5, seq, 0x5A]
    let t0 = mach_absolute_time()
    sentAt[seq] = t0
    let rc = IOHIDDeviceSetReport(dev, kIOHIDReportTypeOutput, 0x03, r, r.count)
    setReportMs.append((ns(mach_absolute_time()) - ns(t0)) / 1e6)
    if rc != kIOReturnSuccess { print(String(format: "SetReport failed 0x%08x", rc)) }
    outstanding = true; lastSend = t0
}

let start = mach_absolute_time()
var sent = 0
// Poll from a 0.5 ms timer: send the next ping gapMs (+ random 0-4 ms, so pings do not phase-lock to the
// laptop's 4 ms stream) after the previous answer, or after a 200 ms timeout (counted as lost).
var nextAt = mach_absolute_time()
let timer = CFRunLoopTimerCreateWithHandler(kCFAllocatorDefault, CFAbsoluteTimeGetCurrent(), 0.0005, 0, 0) { _ in
    let now = mach_absolute_time()
    if outstanding {
        if (ns(now) - ns(lastSend)) / 1e6 > 200 { lost += 1; outstanding = false; sentAt.removeAll() } else { return }
        nextAt = now
    }
    if sent >= total { CFRunLoopStop(CFRunLoopGetCurrent()); return }
    if rtts.count + lost == sent && now >= nextAt {
        sendPing(); sent += 1
        let jitter = Double.random(in: 0..<4)
        nextAt = now + UInt64((gapMs + jitter) * 1e6 * Double(tb.denom) / Double(tb.numer))
    }
}
CFRunLoopAddTimer(CFRunLoopGetCurrent(), timer, .defaultMode)
CFRunLoopRun()
CFRunLoopRunInMode(.defaultMode, 0.3, false)

func pct(_ a: [Double], _ p: Double) -> Double {
    if a.isEmpty { return .nan }
    let s = a.sorted(); return s[min(s.count - 1, Int(Double(s.count - 1) * p + 0.5))]
}
func line(_ name: String, _ a: [Double]) -> String {
    String(format: "%@ n=%d  min %.2f  p50 %.2f  p90 %.2f  p95 %.2f  p99 %.2f  max %.2f  mean %.2f ms", name, a.count,
           a.min() ?? .nan, pct(a, 0.5), pct(a, 0.9), pct(a, 0.95), pct(a, 0.99), a.max() ?? .nan,
           a.isEmpty ? .nan : a.reduce(0, +) / Double(a.count))
}
let secs = (ns(mach_absolute_time()) - ns(start)) / 1e9
print(line("rtt      ", rtts))
print(line("setreport", setReportMs))
print(String(format: "lost %d of %d;  report 1: %.1f/s over %.1f s", lost, sent, Double(r1Count) / secs, secs))
print(line("r1 gap   ", r1Gaps))
var hist = [Int](repeating: 0, count: 12)
for v in r1Gaps { hist[min(11, Int(v))] += 1 }
print("r1 gap histogram (1 ms bins, last = 11+): " + hist.map(String.init).joined(separator: " "))
if let p = csvPath {
    try? rtts.map { String(format: "%.3f", $0) }.joined(separator: "\n").write(toFile: p, atomically: true, encoding: .utf8)
}
