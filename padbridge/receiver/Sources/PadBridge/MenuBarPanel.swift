import SwiftUI

struct MenuBarPanel: View {
    @Environment(BridgeModel.self) private var model
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 10) {
                ControllerBadge(connected: model.isConnected, size: 34)
                VStack(alignment: .leading, spacing: 1) {
                    Text(model.isConnected ? model.title : "No controller connected")
                        .font(.headline)
                    Text(statusLine)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
                if let rssi = model.rssi, model.isConnected {
                    Image(systemName: "cellularbars", variableValue: SignalQuality(rssi: rssi).level)
                        .foregroundStyle(.secondary)
                        .help("\(SignalQuality(rssi: rssi).label) signal, \(rssi) dBm")
                        .accessibilityLabel("\(SignalQuality(rssi: rssi).label) signal")
                }
            }

            if model.identity?.supportsPing == true {
                VStack(alignment: .leading, spacing: 6) {
                    HStack(alignment: .firstTextBaseline) {
                        Text("Latency")
                        Spacer()
                        if let median = model.latency.median {
                            Text("\(formatMs(model.latency.last)) ms")
                                .font(.body.weight(.semibold).monospacedDigit())
                            Text("median \(formatMs(median))")
                                .font(.caption.monospacedDigit())
                                .foregroundStyle(.secondary)
                        } else if model.isMock {
                            Text("Preview").foregroundStyle(.secondary)
                        } else if model.latency.hostSilent {
                            Text("No reply").foregroundStyle(.secondary)
                        } else {
                            Text("Measuring…").foregroundStyle(.secondary)
                        }
                    }
                    if model.latency.hasReplies {
                        LatencyChart(samples: model.latency.samples, median: nil, compact: true)
                            .frame(height: 34)
                    }
                }
                .padding(10)
                .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
            } else if model.identity == .dualShock4 {
                Text("Latency needs Xbox mode.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } else {
                Text("Start PadBridge Host on your Linux laptop.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }

            Divider()

            HStack {
                Text("Open at login")
                Spacer()
                Toggle("Open at login", isOn: Binding(get: { model.openAtLogin }, set: { model.setOpenAtLogin($0) }))
                    .toggleStyle(.switch)
                    .controlSize(.small)
                    .labelsHidden()
            }

            Divider()

            HStack {
                Button("Open PadBridge") {
                    openWindow(id: "main")
                    NSApp.activate()
                }
                .keyboardShortcut(.defaultAction)
                Spacer()
                Button("Quit") { NSApp.terminate(nil) }
                    .keyboardShortcut("q")
            }
        }
        .padding(14)
        .frame(width: 300)
        .background(WindowVisibilityReader { model.setVisible("menu", $0) })
        .onAppear { model.refreshLoginItem() }
    }

    private var statusLine: String {
        guard let device = model.device else { return "Waiting for the laptop" }
        if model.physicalName != nil { return "Appearing as \(device.identity.displayName)" }
        return "Connected · \(device.identity.modeName) mode"
    }
}

struct PairingSheet: View {
    @Environment(\.dismiss) private var dismiss

    private let steps: [(String, String)] = [
        ("Start PadBridge Host on your Linux laptop",
         "Choose how the controller should appear: Xbox or DualShock 4. The laptop becomes discoverable."),
        ("Open Bluetooth settings on this Mac",
         "The laptop shows up under the name of the controller plugged into it, such as 8BitDo Ultimate C."),
        ("Click Connect",
         "Pairing finishes on its own. There’s no code to enter."),
        ("You’re set",
         "From now on the laptop reconnects to this Mac by itself whenever PadBridge Host is running."),
    ]

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(spacing: 12) {
                ControllerBadge(connected: true, size: 44)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Pair a new host").font(.title2.weight(.semibold))
                    Text("Use a Linux laptop as a Bluetooth controller for this Mac.")
                        .foregroundStyle(.secondary)
                }
            }

            VStack(alignment: .leading, spacing: 14) {
                ForEach(Array(steps.enumerated()), id: \.offset) { index, step in
                    HStack(alignment: .firstTextBaseline, spacing: 12) {
                        Text("\(index + 1)")
                            .font(.callout.weight(.semibold).monospacedDigit())
                            .foregroundStyle(.white)
                            .frame(width: 22, height: 22)
                            .background(Circle().fill(Color.accentColor))
                            .alignmentGuide(.firstTextBaseline) { $0[.bottom] - 6 }
                        VStack(alignment: .leading, spacing: 2) {
                            Text(step.0).font(.headline)
                            Text(step.1).foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    .accessibilityElement(children: .combine)
                }
            }

            Label {
                Text("Switching between Xbox and DualShock 4, or seeing an old name? Use Forget This Device in System Settings > Bluetooth, then connect again.")
                    .fixedSize(horizontal: false, vertical: true)
            } icon: {
                Image(systemName: "info.circle")
            }
            .font(.callout)
            .foregroundStyle(.secondary)

            HStack {
                Spacer()
                Button("Open Bluetooth settings") { BluetoothLink.openBluetoothSettings() }
                Button("Done") { dismiss() }
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(24)
        .frame(width: 460)
    }
}
