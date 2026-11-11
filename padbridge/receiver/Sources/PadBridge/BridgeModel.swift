import AppKit
import CoreBluetooth
import GameController
import IOBluetooth
import Observation
import ServiceManagement

struct LatencySample: Identifiable, Equatable {
    let id: Int
    let ms: Double
}

/// Rolling latency statistics over the last 10 seconds of replies.
struct LatencyStats: Equatable {
    static let window = 200

    private(set) var samples: [LatencySample] = []
    private(set) var last: Double?
    private(set) var median: Double?
    private(set) var p95: Double?
    private(set) var sent = 0
    private(set) var lost = 0
    private(set) var consecutiveLost = 0
    private var nextID = 0

    var hasReplies: Bool { last != nil }
    /// True when pings keep timing out and nothing ever came back.
    var hostSilent: Bool { consecutiveLost >= 8 }
    var lossPercent: Double { sent == 0 ? 0 : Double(lost) / Double(sent) * 100 }

    mutating func record(_ ms: Double) {
        sent += 1
        consecutiveLost = 0
        last = ms
        samples.append(LatencySample(id: nextID, ms: ms))
        nextID += 1
        if samples.count > Self.window { samples.removeFirst(samples.count - Self.window) }
        let sorted = samples.map(\.ms).sorted()
        median = Self.percentile(sorted, 0.5)
        p95 = Self.percentile(sorted, 0.95)
    }

    mutating func recordLoss() {
        sent += 1
        lost += 1
        consecutiveLost += 1
    }

    static func percentile(_ sorted: [Double], _ p: Double) -> Double? {
        guard !sorted.isEmpty else { return nil }
        let rank = Int((p * Double(sorted.count)).rounded(.up)) - 1
        return sorted[min(max(rank, 0), sorted.count - 1)]
    }
}

enum RumbleStatus: Equatable {
    case sent(Motor)
    case failed(Motor, Int32)
}

@MainActor
@Observable
final class BridgeModel {
    // Connection
    private(set) var device: BridgedDevice?
    private(set) var bluetoothName: String?
    private(set) var rssi: Int?
    private(set) var bluetoothOn = true
    private(set) var bluetoothAccess: CBManagerAuthorization = .notDetermined
    private(set) var pairedHostAddress: String?
    /// Addresses known to be PadBridge hosts: the development laptop plus any device seen with a non-stock name.
    private(set) var bridgeAddresses: Set<String> = Set(UserDefaults.standard.stringArray(forKey: "bridgeAddresses") ?? [])
        .union(["AA:BB:CC:DD:EE:02"])
    private(set) var linkBusy = false
    private(set) var linkError: String?

    // Input
    private(set) var hasGamepad = false
    private(set) var input = InputState()
    private(set) var glyphs = PadGlyphs.fallback

    // Latency and rumble
    private(set) var latency = LatencyStats()
    private(set) var isSampling = false
    private(set) var rumbleStatus: RumbleStatus?

    // App
    private(set) var loginItemStatus: SMAppService.Status = .notRegistered
    private(set) var loginItemError: String?
    var showPairingSheet = false

    var isConnected: Bool { device != nil }
    var identity: ControllerIdentity? { device?.identity }

    /// Debug preview of identities the engine doesn't serve yet:
    /// `-PBMock "xbox-one|8BitDo Ultimate C"`, `-PBMock "ds4|DUALSHOCK 4 Wireless Controller"` or `-PBMock none`.
    @ObservationIgnored let mock: (device: BridgedDevice?, raw: String)? = {
        guard let raw = UserDefaults.standard.string(forKey: "PBMock") else { return nil }
        if raw == "none" { return (nil, raw) }
        let parts = raw.split(separator: "|", maxSplits: 1).map(String.init)
        guard let identity = ControllerIdentity(rawValue: parts[0]) else { return nil }
        return (BridgedDevice(identity: identity, productName: parts.count > 1 ? parts[1] : identity.displayName,
                              address: "AA:BB:CC:DD:EE:02"), raw)
    }()
    var isMock: Bool { mock != nil }

    /// The name in the Bluetooth device list (IOBluetooth). In DualShock 4 mode macOS's driver renames the
    /// HID product and GameController vendorName, but the Bluetooth list keeps the physical controller's name.
    var deviceName: String? { bluetoothName ?? device?.productName }

    /// The physical controller on the laptop, such as "8BitDo Ultimate C". Nil when macOS only has a stock name.
    var physicalName: String? {
        guard let name = deviceName, !name.isEmpty, !ControllerIdentity.stockNames.contains(name) else { return nil }
        return name
    }

    /// A stock name cached for a known PadBridge host, left over from an earlier pairing.
    /// Forget This Device in Bluetooth settings, then connecting again, refreshes it.
    var staleName: String? {
        guard let name = deviceName, ControllerIdentity.stockNames.contains(name),
              let address = device?.address, bridgeAddresses.contains(address) else { return nil }
        return name
    }

    /// Title for the device: the physical controller when known, else the emulated identity.
    var title: String { physicalName ?? identity?.displayName ?? "Controller" }

    @ObservationIgnored private let hid = HIDLink()
    @ObservationIgnored private var gamepad: GCController?
    @ObservationIgnored private var viewers: Set<String> = []
    @ObservationIgnored private var inputTimer: Timer?
    @ObservationIgnored private var statusTimer: Timer?
    @ObservationIgnored private var rumbleClear: Task<Void, Never>?
    @ObservationIgnored private var waiter: ConnectionWaiter?
    @ObservationIgnored private var observers: [NSObjectProtocol] = []
    /// Pings land here at 20 per second; the UI copy is refreshed 5 times a second to keep redraws cheap.
    @ObservationIgnored private var liveLatency = LatencyStats()
    @ObservationIgnored private var latencyPublisher: Timer?
    @ObservationIgnored private lazy var bluetoothPermission = BluetoothPermission { [weak self] in self?.refreshBluetooth() }

    init() {
        pairedHostAddress = UserDefaults.standard.string(forKey: "lastAddress")
        GCController.shouldMonitorBackgroundEvents = true

        hid.onDeviceChange = { [weak self] info in
            DispatchQueue.main.async { MainActor.assumeIsolated { self?.deviceChanged(info) } }
        }
        hid.onPing = { [weak self] result in
            DispatchQueue.main.async { MainActor.assumeIsolated { self?.pingResult(result) } }
        }
        hid.start()

        let center = NotificationCenter.default
        for name in [Notification.Name.GCControllerDidConnect, .GCControllerDidDisconnect] {
            observers.append(center.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.pickGamepad() }
            })
        }
        pickGamepad()
        refreshLoginItem()
        DispatchQueue.main.async { MainActor.assumeIsolated { self.refreshBluetooth() } }
        statusTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.refreshBluetooth() }
        }
    }

    // MARK: Visibility-driven activity

    /// Windows report whether they are on screen; sampling and fast polling run only while one is.
    func setVisible(_ viewer: String, _ visible: Bool) {
        if visible { viewers.insert(viewer) } else { viewers.remove(viewer) }
        updateActivity()
    }

    private func updateActivity() {
        let active = !viewers.isEmpty && isConnected
        let sample = active && identity?.supportsPing == true && !isMock
        if sample != isSampling {
            isSampling = sample
            hid.setPinging(sample)
            latencyPublisher?.invalidate()
            latencyPublisher = nil
            if sample {
                latencyPublisher = Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { [weak self] _ in
                    MainActor.assumeIsolated {
                        guard let self, self.latency != self.liveLatency else { return }
                        self.latency = self.liveLatency
                    }
                }
            }
        }
        if active, inputTimer == nil {
            inputTimer = Timer.scheduledTimer(withTimeInterval: 1.0 / 60, repeats: true) { [weak self] _ in
                MainActor.assumeIsolated { self?.pollInput() }
            }
            refreshBluetooth()
        } else if !active {
            inputTimer?.invalidate()
            inputTimer = nil
        }
    }

    // MARK: Device

    private func deviceChanged(_ live: BridgedDevice?) {
        let info = mock.map(\.device) ?? live
        let previous = device
        device = info
        if let address = info?.address, !isMock {
            pairedHostAddress = address
            UserDefaults.standard.set(address, forKey: "lastAddress")
        }
        if previous?.identity != info?.identity || previous == nil {
            latency = LatencyStats()
            liveLatency = LatencyStats()
        }
        linkError = nil
        pickGamepad()
        refreshBluetooth()
        updateActivity()
    }

    private func pickGamepad() {
        let pads = GCController.controllers().filter { $0.extendedGamepad != nil }
        let name = device?.productName
        let chosen = pads.first { $0.vendorName == name && name != nil }
            ?? pads.first { BluetoothLink.knownNames.contains($0.vendorName ?? "") }
            ?? pads.first { ["Xbox One", "DualShock 4"].contains($0.productCategory) && $0.vendorName != "GamePad-1" }
        gamepad = chosen
        hasGamepad = chosen != nil
        if let gp = chosen?.extendedGamepad {
            glyphs = PadGlyphs(gp)
            input = InputState(gp)
        } else {
            input = InputState()
        }
    }

    private func pollInput() {
        guard let gp = gamepad?.extendedGamepad else { return }
        let state = InputState(gp)
        if state != input { input = state }
    }

    private func refreshBluetooth() {
        let access = BluetoothLink.authorization
        if access != bluetoothAccess { NSLog("PadBridge: Bluetooth access %ld", access.rawValue) }
        bluetoothAccess = access
        if bluetoothAccess == .notDetermined {
            // Never call IOBluetooth while undecided: it blocks the main thread. Ask through CoreBluetooth instead.
            bluetoothPermission.request()
        }
        bluetoothOn = BluetoothLink.isPoweredOn
        if isMock {
            bluetoothName = device?.productName
            rssi = device == nil ? nil : -52
            return
        }
        if let address = device?.address {
            bluetoothName = BluetoothLink.name(address) ?? device?.productName
            if physicalName != nil, !bridgeAddresses.contains(address) {
                bridgeAddresses.insert(address)
                UserDefaults.standard.set(Array(bridgeAddresses), forKey: "bridgeAddresses")
            }
            rssi = viewers.isEmpty ? rssi : BluetoothLink.rssi(address)
        } else {
            bluetoothName = nil
            rssi = nil
            if let host = BluetoothLink.pairedHost(preferring: pairedHostAddress), let address = host.addressString {
                pairedHostAddress = HIDLink.normalize(address)
            }
        }
    }

    // MARK: Latency

    private func pingResult(_ result: PingResult) {
        guard isSampling else { return }
        switch result {
        case .reply(let ms): liveLatency.record(ms)
        case .timeout, .sendFailed: liveLatency.recordLoss()
        }
    }

    // MARK: Rumble

    func rumble(_ motor: Motor) {
        if isMock { rumbleStatus = .sent(motor); return }
        hid.rumble(motor) { [weak self] result in
            DispatchQueue.main.async {
                MainActor.assumeIsolated {
                    guard let self else { return }
                    self.rumbleStatus = result == kIOReturnSuccess ? .sent(motor) : .failed(motor, result)
                    self.rumbleClear?.cancel()
                    self.rumbleClear = Task { [weak self] in
                        try? await Task.sleep(for: .seconds(3))
                        if !Task.isCancelled { self?.rumbleStatus = nil }
                    }
                }
            }
        }
    }

    // MARK: Connect and disconnect

    var canManageLink: Bool { bluetoothAccess == .allowedAlways && bluetoothOn }

    func disconnect() {
        guard let address = device?.address, let bt = BluetoothLink.device(address) else { return }
        let result = bt.closeConnection()
        if result != kIOReturnSuccess {
            linkError = String(format: "Couldn’t disconnect (error 0x%08X). Try again from Bluetooth settings.", result)
        }
    }

    func connect() {
        guard !linkBusy, let address = pairedHostAddress, let bt = BluetoothLink.device(address) else { return }
        linkBusy = true
        linkError = nil
        let waiter = ConnectionWaiter { [weak self] status in
            MainActor.assumeIsolated {
                self?.linkBusy = false
                if status != kIOReturnSuccess {
                    self?.linkError = "Couldn’t reach the laptop. Make sure PadBridge Host is running, then try again."
                }
                self?.waiter = nil
            }
        }
        self.waiter = waiter
        let result = bt.openConnection(waiter, withPageTimeout: 0x2000, authenticationRequired: false)
        if result != kIOReturnSuccess {
            linkBusy = false
            linkError = "Couldn’t reach the laptop. Make sure PadBridge Host is running, then try again."
            self.waiter = nil
        }
    }

    // MARK: Open at login

    var openAtLogin: Bool { loginItemStatus == .enabled || loginItemStatus == .requiresApproval }

    func setOpenAtLogin(_ enabled: Bool) {
        loginItemError = nil
        do {
            if enabled { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
        } catch {
            loginItemError = "Couldn’t change this setting. \(error.localizedDescription)"
        }
        refreshLoginItem()
    }

    func refreshLoginItem() {
        loginItemStatus = SMAppService.mainApp.status
    }
}
