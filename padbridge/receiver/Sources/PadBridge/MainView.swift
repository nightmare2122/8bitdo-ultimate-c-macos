import SwiftUI

struct MainView: View {
    @Environment(BridgeModel.self) private var model

    var body: some View {
        @Bindable var model = model
        Group {
            if model.isConnected {
                ConnectedView()
            } else {
                DisconnectedView()
            }
        }
        .frame(minWidth: 540, idealWidth: 620, minHeight: 480, idealHeight: 900)
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button {
                    model.showPairingSheet = true
                } label: {
                    Label("Pair new host", systemImage: "plus")
                }
                .help("Pair a new PadBridge host")
            }
        }
        .sheet(isPresented: $model.showPairingSheet) {
            PairingSheet()
        }
        .background(WindowVisibilityReader { model.setVisible("main", $0) })
    }
}

// MARK: - Connected

struct ConnectedView: View {
    @Environment(BridgeModel.self) private var model

    var body: some View {
        Form {
            Section {
                StatusHeader()
                ConnectionDetails()
            }
            Section("Input") {
                InputView()
            }
            Section {
                LatencyView()
            } header: {
                Text("Latency")
            } footer: {
                if model.identity?.supportsPing == true {
                    Text("Round trip from this Mac to the laptop and back over Bluetooth, measured 20 times a second while PadBridge is on screen. It doesn’t include the controller’s USB link to the laptop.")
                        .foregroundStyle(.secondary)
                        .font(.footnote)
                        .multilineTextAlignment(.leading)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            Section {
                RumbleView()
            } header: {
                Text("Rumble test")
            }
        }
        .formStyle(.grouped)
    }
}

struct StatusHeader: View {
    @Environment(BridgeModel.self) private var model

    var body: some View {
        HStack(spacing: 14) {
            ControllerBadge(connected: true, size: 52)
            VStack(alignment: .leading, spacing: 3) {
                Text(model.title)
                    .font(.title3.weight(.semibold))
                if model.physicalName != nil, let identity = model.identity {
                    Text("Appearing as \(identity.displayName)")
                        .foregroundStyle(.secondary)
                        .font(.callout)
                }
                HStack(spacing: 5) {
                    Circle().fill(.green).frame(width: 7, height: 7)
                        .accessibilityHidden(true)
                    Text("Connected over Bluetooth")
                        .foregroundStyle(.secondary)
                }
                .font(.callout)
            }
            Spacer()
            if model.isMock {
                Text("Preview")
                    .font(.caption.weight(.semibold))
                    .padding(.horizontal, 6).padding(.vertical, 2)
                    .background(.orange.opacity(0.2), in: Capsule())
                    .help("Showing preview data from the PBMock setting")
            }
            Button("Disconnect") { model.disconnect() }
                .disabled(!model.canManageLink)
                .help(model.canManageLink ? "Disconnect the laptop from this Mac" : "Allow Bluetooth access to disconnect from here")
        }
        .padding(.vertical, 4)
        .accessibilityElement(children: .contain)
    }
}

struct ConnectionDetails: View {
    @Environment(BridgeModel.self) private var model

    var body: some View {
        if let device = model.device {
            LabeledContent("Appears as") {
                Text("\(device.identity.modeName)  ·  \(device.identity.idString)")
                    .monospacedDigit()
            }
            if let address = device.address {
                LabeledContent("Bluetooth address") {
                    Text(address).monospaced().textSelection(.enabled)
                }
            }
            LabeledContent("Signal") {
                SignalView(rssi: model.rssi, access: model.bluetoothAccess)
            }
            if let stale = model.staleName {
                StaleNameNotice(staleName: stale, identity: device.identity)
            } else if let name = model.deviceName, model.physicalName == nil, name != device.identity.displayName {
                LabeledContent("Name in Bluetooth settings", value: name)
            }
            if let error = model.linkError {
                Text(error).font(.callout).foregroundStyle(.red)
            }
        }
    }
}

/// macOS caches the Bluetooth name from the first pairing. If it names a different kind of controller
/// than the one in use, forgetting and re-pairing the device refreshes it.
struct StaleNameNotice: View {
    let staleName: String
    let identity: ControllerIdentity
    @Environment(BridgeModel.self) private var model

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "info.circle")
                .foregroundStyle(.secondary)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 6) {
                Text("macOS still calls this device “\(staleName)”")
                    .font(.headline)
                Text("That name is left over from an earlier pairing. To refresh it, use Forget This Device in System Settings > Bluetooth, then connect again.")
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Button("Open Bluetooth settings") { BluetoothLink.openBluetoothSettings() }
                    .controlSize(.small)
                    .padding(.top, 2)
            }
        }
        .padding(.vertical, 4)
        .accessibilityElement(children: .contain)
    }
}

struct SignalView: View {
    let rssi: Int?
    let access: CBManagerAuthorizationAlias

    var body: some View {
        if let rssi {
            let quality = SignalQuality(rssi: rssi)
            HStack(spacing: 6) {
                Image(systemName: "cellularbars", variableValue: quality.level)
                    .foregroundStyle(quality.level < 0.5 ? AnyShapeStyle(.orange) : AnyShapeStyle(.primary))
                    .accessibilityHidden(true)
                Text("\(quality.label)  ·  \(rssi) dBm").monospacedDigit()
            }
            .accessibilityElement(children: .combine)
        } else if access == .denied || access == .restricted {
            Button("Allow Bluetooth access") { BluetoothLink.openPrivacySettings() }
                .buttonStyle(.link)
        } else {
            Text("Measuring…").foregroundStyle(.secondary)
        }
    }
}

struct SignalQuality {
    let level: Double
    let label: String

    init(rssi: Int) {
        switch rssi {
        case (-60)...: (level, label) = (1.0, "Excellent")
        case (-70)...: (level, label) = (0.75, "Good")
        case (-80)...: (level, label) = (0.5, "Fair")
        default: (level, label) = (0.25, "Weak")
        }
    }
}

/// The app's controller glyph on a tinted tile.
struct ControllerBadge: View {
    let connected: Bool
    var size: CGFloat = 52

    var body: some View {
        RoundedRectangle(cornerRadius: size * 0.26, style: .continuous)
            .fill(connected ? AnyShapeStyle(Color.accentColor.gradient) : AnyShapeStyle(Color.secondary.opacity(0.18)))
            .frame(width: size, height: size)
            .overlay {
                Image(systemName: "gamecontroller.fill")
                    .font(.system(size: size * 0.46, weight: .medium))
                    .foregroundStyle(connected ? AnyShapeStyle(.white) : AnyShapeStyle(.secondary))
            }
            .accessibilityHidden(true)
    }
}

// MARK: - Disconnected

struct DisconnectedView: View {
    @Environment(BridgeModel.self) private var model

    var body: some View {
        VStack(spacing: 0) {
            Spacer(minLength: 24)
            if !model.bluetoothOn {
                ContentUnavailableView {
                    Label("Bluetooth is off", systemImage: "antenna.radiowaves.left.and.right.slash")
                } description: {
                    Text("Turn on Bluetooth to use your bridged controller.")
                } actions: {
                    Button("Open Bluetooth settings") { BluetoothLink.openBluetoothSettings() }
                        .buttonStyle(.borderedProminent)
                }
            } else {
                ContentUnavailableView {
                    Label {
                        Text("No controller connected")
                    } icon: {
                        ControllerBadge(connected: false, size: 64)
                    }
                } description: {
                    Text("Start PadBridge Host on your Linux laptop. It connects to this Mac by itself within a few seconds.")
                } actions: {
                    VStack(spacing: 10) {
                        if model.pairedHostAddress != nil && model.canManageLink {
                            Button {
                                model.connect()
                            } label: {
                                if model.linkBusy {
                                    HStack(spacing: 6) { ProgressView().controlSize(.small); Text("Connecting…") }
                                } else {
                                    Text("Connect now")
                                }
                            }
                            .buttonStyle(.borderedProminent)
                            .disabled(model.linkBusy)
                        }
                        HStack {
                            Button("Pair new host…") { model.showPairingSheet = true }
                            Button("Open Bluetooth settings") { BluetoothLink.openBluetoothSettings() }
                        }
                        .fixedSize()
                        if let error = model.linkError {
                            Text(error).font(.callout).foregroundStyle(.secondary)
                                .multilineTextAlignment(.center)
                        }
                        if model.bluetoothAccess == .denied || model.bluetoothAccess == .restricted {
                            Button("Allow Bluetooth access to connect from PadBridge") { BluetoothLink.openPrivacySettings() }
                                .buttonStyle(.link)
                                .font(.callout)
                        }
                    }
                }
            }
            Spacer(minLength: 24)
            if let address = model.pairedHostAddress {
                Text("Last connected host: \(address)")
                    .font(.footnote.monospaced())
                    .foregroundStyle(.tertiary)
                    .padding(.bottom, 16)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

import CoreBluetooth
typealias CBManagerAuthorizationAlias = CBManagerAuthorization
