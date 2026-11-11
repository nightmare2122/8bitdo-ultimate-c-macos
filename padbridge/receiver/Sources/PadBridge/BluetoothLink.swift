import AppKit
import CoreBluetooth
import IOBluetooth

/// Thin wrapper around IOBluetooth for signal strength and connect/disconnect.
/// IOBluetooth needs the Bluetooth privacy permission; everything else in the app works without it.
@MainActor
enum BluetoothLink {
    static var knownNames: Set<String> { ControllerIdentity.stockNames }

    static var authorization: CBManagerAuthorization { CBManager.authorization }

    static var isAllowed: Bool { authorization == .allowedAlways }

    static var isPoweredOn: Bool {
        guard isAllowed, let host = IOBluetoothHostController.default() else { return true }
        return host.powerState == kBluetoothHCIPowerStateON
    }

    static func device(_ address: String) -> IOBluetoothDevice? {
        guard isAllowed else { return nil }
        return IOBluetoothDevice(addressString: address)
    }

    /// RSSI in dBm, or nil when unavailable.
    static func rssi(_ address: String) -> Int? {
        guard let device = device(address), device.isConnected() else { return nil }
        let raw = Int(device.rawRSSI())
        if raw != 127, raw != 0 { return raw }
        let rssi = Int(device.rssi())
        return rssi == 127 ? nil : rssi
    }

    static func name(_ address: String) -> String? { device(address)?.name }

    /// A paired PadBridge host, preferring the address seen last time.
    static func pairedHost(preferring address: String?) -> IOBluetoothDevice? {
        guard isAllowed, let paired = IOBluetoothDevice.pairedDevices() as? [IOBluetoothDevice] else { return nil }
        if let address, let match = paired.first(where: { HIDLink.normalize($0.addressString ?? "") == address }) {
            return match
        }
        return paired.first { knownNames.contains($0.name ?? "") }
    }

    static func openBluetoothSettings() {
        NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.BluetoothSettings")!)
    }

    static func openPrivacySettings() {
        NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Bluetooth")!)
    }
}

/// Receives IOBluetooth's asynchronous connection result.
final class ConnectionWaiter: NSObject {
    private let done: (IOReturn) -> Void
    init(done: @escaping (IOReturn) -> Void) { self.done = done }
    @objc func connectionComplete(_ device: IOBluetoothDevice!, status: IOReturn) { done(status) }
}

/// Asks for the Bluetooth permission without blocking. IOBluetooth blocks the calling thread while the
/// permission is undecided, so the app only touches IOBluetooth after this reports `.allowedAlways`.
final class BluetoothPermission: NSObject, CBCentralManagerDelegate, @unchecked Sendable {
    private var manager: CBCentralManager?
    private let onChange: @MainActor () -> Void

    init(onChange: @escaping @MainActor () -> Void) {
        self.onChange = onChange
        super.init()
    }

    func request() {
        guard manager == nil else { return }
        manager = CBCentralManager(delegate: self, queue: .main, options: [CBCentralManagerOptionShowPowerAlertKey: false])
    }

    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        MainActor.assumeIsolated { onChange() }
    }
}
