import Foundation
import IOKit.hid

/// Which controller the Linux host is posing as (contract Addendum A; xbox-series is paused and not supported).
enum ControllerIdentity: String, Sendable, CaseIterable {
    case xboxOne = "xbox-one"
    case dualShock4 = "ds4"

    /// What macOS believes the device is.
    var displayName: String {
        switch self {
        case .xboxOne: "Xbox Wireless Controller"
        case .dualShock4: "DualShock 4"
        }
    }

    var modeName: String {
        switch self {
        case .xboxOne: "Xbox One S"
        case .dualShock4: "DualShock 4"
        }
    }

    /// Names macOS gives a stock controller (or the bridge before a re-pair).
    static let stockNames: Set<String> = ["Xbox Wireless Controller", "DUALSHOCK 4 Wireless Controller", "Wireless Controller"]

    var isXbox: Bool { self == .xboxOne }
    /// The latency ping (report 3 out, report 4 in) only exists in Xbox mode.
    var supportsPing: Bool { isXbox }

    var vendorID: Int { isXbox ? 0x045E : 0x054C }
    var productID: Int { isXbox ? 0x02FD : 0x09CC }

    var idString: String { String(format: "%04X:%04X", vendorID, productID) }

    init?(vendorID: Int, productID: Int) {
        guard let match = Self.allCases.first(where: { $0.vendorID == vendorID && $0.productID == productID }) else { return nil }
        self = match
    }
}

/// The bridged controller as IOHID sees it.
struct BridgedDevice: Sendable, Equatable {
    var identity: ControllerIdentity
    /// Product string, which is the cached Bluetooth name (it can lag behind the identity).
    var productName: String
    /// Bluetooth address in "AA:BB:CC:DD:EE:02" form, if known.
    var address: String?
}

enum PingResult: Sendable {
    case reply(ms: Double)
    case timeout
    case sendFailed(Int32)
}

enum Motor: String, Sendable, CaseIterable, Identifiable {
    case strong, weak, both
    var id: String { rawValue }
}

/// Owns an IOHIDManager on a dedicated high-priority thread so that latency pings are
/// timed independently of UI work on the main thread. All mutable state is touched only
/// on that thread; the public methods hop onto it.
final class HIDLink: @unchecked Sendable {
    static let pingInterval: Double = 0.05      // 20 per second
    static let pingTimeout: Double = 0.25       // 250 ms
    static let rumbleDuration: Double = 0.4

    var onDeviceChange: (@Sendable (BridgedDevice?) -> Void)?
    var onPing: (@Sendable (PingResult) -> Void)?

    private var runLoop: CFRunLoop?
    private var manager: IOHIDManager?
    private var devices: [IOHIDDevice] = []
    private var active: IOHIDDevice?
    private var activeIdentity: ControllerIdentity?
    private let reportBuffer = UnsafeMutablePointer<UInt8>.allocate(capacity: 512)
    private var pingEnabled = false
    private var sequence: UInt8 = 0
    private var outstanding: (seq: UInt8, sentAt: UInt64)?
    private var pingTimer: CFRunLoopTimer?
    private let ticksPerMs: Double

    init() {
        var tb = mach_timebase_info_data_t()
        mach_timebase_info(&tb)
        ticksPerMs = 1_000_000 * Double(tb.denom) / Double(tb.numer)
    }

    func start() {
        let ready = DispatchSemaphore(value: 0)
        let thread = Thread { [self] in
            runLoop = CFRunLoopGetCurrent()
            setUp()
            ready.signal()
            while true { CFRunLoopRun() }
        }
        thread.name = "PadBridge HID"
        thread.qualityOfService = .userInteractive
        thread.start()
        ready.wait()
    }

    func setPinging(_ enabled: Bool) {
        perform { [self] in
            pingEnabled = enabled
            if !enabled { outstanding = nil }
        }
    }

    func rumble(_ motor: Motor, completion: @escaping @Sendable (Int32) -> Void) {
        perform { [self] in
            guard let dev = active, let identity = activeIdentity else {
                completion(kIOReturnNotAttached)
                return
            }
            let strong = motor != .weak
            let weak = motor != .strong
            switch identity {
            case .xboxOne:
                // enable (0x01 weak, 0x02 strong), LT, RT, strong, weak (0-100), duration, delay (10 ms), loop
                let enable: UInt8 = (strong ? 0x02 : 0) | (weak ? 0x01 : 0)
                let report: [UInt8] = [0x03, enable, 0, 0, strong ? 70 : 0, weak ? 70 : 0,
                                       UInt8(Self.rumbleDuration * 100), 0, 0]
                completion(IOHIDDeviceSetReport(dev, kIOHIDReportTypeOutput, 0x03, report, report.count))
            case .dualShock4:
                let result = sendDS4(dev, weak: weak ? 180 : 0, strong: strong ? 180 : 0)
                completion(result)
                guard result == kIOReturnSuccess else { return }
                let stop = CFRunLoopTimerCreateWithHandler(nil, CFAbsoluteTimeGetCurrent() + Self.rumbleDuration, 0, 0, 0) { [self] _ in
                    _ = sendDS4(dev, weak: 0, strong: 0)
                }
                CFRunLoopAddTimer(runLoop, stop, .defaultMode)
            }
        }
    }

    // MARK: - HID thread

    private func perform(_ block: @escaping @Sendable () -> Void) {
        guard let runLoop else { return }
        CFRunLoopPerformBlock(runLoop, CFRunLoopMode.defaultMode.rawValue, block)
        CFRunLoopWakeUp(runLoop)
    }

    private func setUp() {
        let manager = IOHIDManagerCreate(kCFAllocatorDefault, IOOptionBits(kIOHIDOptionsTypeNone))
        // Transport is checked in deviceAdded (Bluetooth only, never USB).
        let matches: [[String: Any]] = ControllerIdentity.allCases.map {
            [kIOHIDVendorIDKey: $0.vendorID, kIOHIDProductIDKey: $0.productID]
        }
        IOHIDManagerSetDeviceMatchingMultiple(manager, matches as CFArray)
        let context = Unmanaged.passUnretained(self).toOpaque()
        IOHIDManagerRegisterDeviceMatchingCallback(manager, { context, _, _, device in
            Unmanaged<HIDLink>.fromOpaque(context!).takeUnretainedValue().deviceAdded(device)
        }, context)
        IOHIDManagerRegisterDeviceRemovalCallback(manager, { context, _, _, device in
            Unmanaged<HIDLink>.fromOpaque(context!).takeUnretainedValue().deviceRemoved(device)
        }, context)
        IOHIDManagerScheduleWithRunLoop(manager, CFRunLoopGetCurrent(), CFRunLoopMode.defaultMode.rawValue)
        IOHIDManagerOpen(manager, IOOptionBits(kIOHIDOptionsTypeNone))
        self.manager = manager

        let timer = CFRunLoopTimerCreateWithHandler(nil, CFAbsoluteTimeGetCurrent() + Self.pingInterval,
                                                    Self.pingInterval, 0, 0) { [self] _ in tick() }
        CFRunLoopAddTimer(CFRunLoopGetCurrent(), timer, .defaultMode)
        pingTimer = timer
        publish()
    }

    private func identity(of device: IOHIDDevice) -> ControllerIdentity? {
        let vid = IOHIDDeviceGetProperty(device, kIOHIDVendorIDKey as CFString) as? Int ?? 0
        let pid = IOHIDDeviceGetProperty(device, kIOHIDProductIDKey as CFString) as? Int ?? 0
        return ControllerIdentity(vendorID: vid, productID: pid)
    }

    private func deviceAdded(_ device: IOHIDDevice) {
        let transport = IOHIDDeviceGetProperty(device, kIOHIDTransportKey as CFString) as? String ?? ""
        guard identity(of: device) != nil, transport.hasPrefix("Bluetooth"), !devices.contains(device) else { return }
        devices.append(device)
        let context = Unmanaged.passUnretained(self).toOpaque()
        IOHIDDeviceRegisterInputReportWithTimeStampCallback(device, reportBuffer, 512, {
            context, _, _, _, reportID, report, length, timeStamp in
            Unmanaged<HIDLink>.fromOpaque(context!).takeUnretainedValue()
                .inputReport(id: reportID, report: report, length: length, timeStamp: timeStamp)
        }, context)
        selectActive()
    }

    private func deviceRemoved(_ device: IOHIDDevice) {
        devices.removeAll { $0 == device }
        selectActive()
    }

    private func selectActive() {
        let newest = devices.last
        if newest != active {
            active = newest
            activeIdentity = newest.flatMap(identity(of:))
            outstanding = nil
        }
        publish()
    }

    private func publish() {
        let info = active.flatMap { dev -> BridgedDevice? in
            guard let identity = identity(of: dev) else { return nil }
            let name = IOHIDDeviceGetProperty(dev, kIOHIDProductKey as CFString) as? String ?? identity.displayName
            let serial = IOHIDDeviceGetProperty(dev, kIOHIDSerialNumberKey as CFString) as? String
            return BridgedDevice(identity: identity, productName: name, address: serial.map(Self.normalize))
        }
        onDeviceChange?(info)
    }

    static func normalize(_ address: String) -> String {
        address.replacingOccurrences(of: "-", with: ":").uppercased()
    }

    private func tick() {
        guard pingEnabled, let dev = active, activeIdentity?.supportsPing == true else { return }
        let now = mach_absolute_time()
        if let pending = outstanding {
            if Double(now - pending.sentAt) / ticksPerMs < Self.pingTimeout * 1000 { return }
            outstanding = nil
            onPing?(.timeout)
        }
        sequence &+= 1
        // Contract: output report 3 = 00 00 00 00 00 A5 <seq> 5A, answered by input report 4 = <seq>.
        let report: [UInt8] = [0x03, 0, 0, 0, 0, 0, 0xA5, sequence, 0x5A]
        outstanding = (sequence, mach_absolute_time())
        let result = IOHIDDeviceSetReport(dev, kIOHIDReportTypeOutput, 0x03, report, report.count)
        if result != kIOReturnSuccess {
            outstanding = nil
            onPing?(.sendFailed(result))
        }
    }

    private func inputReport(id: UInt32, report: UnsafeMutablePointer<UInt8>, length: CFIndex, timeStamp: UInt64) {
        guard id == 4, length >= 1, let pending = outstanding else { return }
        // IOHID usually includes the report ID as the first byte.
        let seq = (length >= 2 && report[0] == 0x04) ? report[1] : report[0]
        guard seq == pending.seq else { return }
        outstanding = nil
        let received = timeStamp > pending.sentAt ? timeStamp : mach_absolute_time()
        onPing?(.reply(ms: Double(received - pending.sentAt) / ticksPerMs))
    }

    private func sendDS4(_ dev: IOHIDDevice, weak: UInt8, strong: UInt8) -> IOReturn {
        // DualShock 4 Bluetooth output report 0x11: flags, rumble (byte 6 weak, 7 strong), light bar, CRC32.
        var report = [UInt8](repeating: 0, count: 78)
        report[0] = 0x11; report[1] = 0xC0; report[2] = 0x20; report[3] = 0x03
        report[6] = weak; report[7] = strong
        report[8] = 0x00; report[9] = 0x40; report[10] = 0xFF
        var crc: UInt32 = 0xFFFF_FFFF
        for byte in [0xA2] + report[0..<74] {
            crc ^= UInt32(byte)
            for _ in 0..<8 { crc = (crc >> 1) ^ (0xEDB8_8320 & (0 &- (crc & 1))) }
        }
        crc = ~crc
        for i in 0..<4 { report[74 + i] = UInt8((crc >> (8 * UInt32(i))) & 0xFF) }
        return IOHIDDeviceSetReport(dev, kIOHIDReportTypeOutput, 0x11, report, report.count)
    }
}
