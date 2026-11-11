import AppKit
import SwiftUI

@main
struct PadBridgeApp: App {
    @State private var model = BridgeModel()

    init() {
        // Debug aid for screenshots: `open PadBridge.app --args -PBAppearance light|dark`.
        switch UserDefaults.standard.string(forKey: "PBAppearance") {
        case "light": NSApplication.shared.appearance = NSAppearance(named: .aqua)
        case "dark": NSApplication.shared.appearance = NSAppearance(named: .darkAqua)
        default: break
        }
    }

    var body: some Scene {
        Window("PadBridge", id: "main") {
            MainView()
                .environment(model)
        }
        .defaultSize(width: 620, height: 980)
        .windowToolbarStyle(.unified)
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("Pair new host…") { model.showPairingSheet = true }
                    .keyboardShortcut("n")
            }
        }

        Settings {
            SettingsView()
                .environment(model)
        }

        MenuBarExtra {
            MenuBarPanel()
                .environment(model)
        } label: {
            MenuBarLabel()
                .environment(model)
        }
        .menuBarExtraStyle(.window)
    }
}

/// Reports whether the hosting window is actually on screen (not closed, minimized or fully covered).
struct WindowVisibilityReader: NSViewRepresentable {
    let onChange: @MainActor (Bool) -> Void

    func makeNSView(context: Context) -> ObserverView { ObserverView(onChange: onChange) }
    func updateNSView(_ nsView: ObserverView, context: Context) {}

    final class ObserverView: NSView {
        let onChange: @MainActor (Bool) -> Void
        private var tokens: [NSObjectProtocol] = []

        init(onChange: @escaping @MainActor (Bool) -> Void) {
            self.onChange = onChange
            super.init(frame: .zero)
        }

        required init?(coder: NSCoder) { fatalError() }

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            tokens.forEach(NotificationCenter.default.removeObserver)
            tokens = []
            guard let window else { onChange(false); return }
            let center = NotificationCenter.default
            tokens.append(center.addObserver(forName: NSWindow.didChangeOcclusionStateNotification, object: window, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.report() }
            })
            tokens.append(center.addObserver(forName: NSWindow.willCloseNotification, object: window, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.onChange(false) }
            })
            report()
        }

        private func report() {
            guard let window else { return }
            onChange(window.isVisible && window.occlusionState.contains(.visible))
        }
    }
}

struct SettingsView: View {
    @Environment(BridgeModel.self) private var model

    var body: some View {
        Form {
            OpenAtLoginRow()
        }
        .formStyle(.grouped)
        .frame(width: 420)
        .fixedSize(horizontal: false, vertical: true)
    }
}

struct OpenAtLoginRow: View {
    @Environment(BridgeModel.self) private var model

    var body: some View {
        Toggle(isOn: Binding(get: { model.openAtLogin }, set: { model.setOpenAtLogin($0) })) {
            Text("Open at login")
            Text("Keeps PadBridge in the menu bar after you restart.")
        }
        .onAppear { model.refreshLoginItem() }
        if model.loginItemStatus == .requiresApproval {
            HStack {
                Label("Allow PadBridge in Login Items to finish turning this on.", systemImage: "exclamationmark.triangle.fill")
                    .symbolRenderingMode(.multicolor)
                    .font(.callout)
                Spacer()
                Button("Open Login Items") { SMAppServiceHelper.openLoginItems() }
            }
        }
        if let error = model.loginItemError {
            Text(error).font(.callout).foregroundStyle(.red)
        }
    }
}

import ServiceManagement
enum SMAppServiceHelper {
    static func openLoginItems() { SMAppService.openSystemSettingsLoginItems() }
}

struct MenuBarLabel: View {
    @Environment(BridgeModel.self) private var model

    var body: some View {
        Image(systemName: model.isConnected ? "gamecontroller.fill" : "gamecontroller")
            .accessibilityLabel(model.isConnected ? "PadBridge, connected" : "PadBridge, not connected")
    }
}
