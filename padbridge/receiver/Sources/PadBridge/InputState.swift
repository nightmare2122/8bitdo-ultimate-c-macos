import GameController

enum PadButton: String, CaseIterable, Hashable {
    case a, b, x, y, lb, rb, lt, rt, ls, rs, view, menu, guide, share, up, down, left, right
}

/// A snapshot of the gamepad, cheap to compare so the UI only redraws on change.
struct InputState: Equatable {
    var leftStick = CGPoint.zero      // -1...1, y up
    var rightStick = CGPoint.zero
    var leftTrigger: Float = 0         // 0...1
    var rightTrigger: Float = 0
    var pressed: Set<PadButton> = []

    init() {}

    @MainActor
    init(_ gp: GCExtendedGamepad) {
        leftStick = CGPoint(x: Double(gp.leftThumbstick.xAxis.value), y: Double(gp.leftThumbstick.yAxis.value))
        rightStick = CGPoint(x: Double(gp.rightThumbstick.xAxis.value), y: Double(gp.rightThumbstick.yAxis.value))
        leftTrigger = gp.leftTrigger.value
        rightTrigger = gp.rightTrigger.value
        let map: [(GCControllerButtonInput?, PadButton)] = [
            (gp.buttonA, .a), (gp.buttonB, .b), (gp.buttonX, .x), (gp.buttonY, .y),
            (gp.leftShoulder, .lb), (gp.rightShoulder, .rb),
            (gp.leftTrigger, .lt), (gp.rightTrigger, .rt),
            (gp.leftThumbstickButton, .ls), (gp.rightThumbstickButton, .rs),
            (gp.buttonOptions, .view), (gp.buttonMenu, .menu), (gp.buttonHome, .guide),
            ((gp as? GCXboxGamepad)?.buttonShare, .share),
            ((gp as? GCDualShockGamepad)?.touchpadButton, .share),
            (gp.dpad.up, .up), (gp.dpad.down, .down), (gp.dpad.left, .left), (gp.dpad.right, .right),
        ]
        for (input, button) in map where input?.isPressed == true { pressed.insert(button) }
    }
}

/// SF Symbol names and spoken names for each control, taken from GameController where it
/// provides them so the glyphs match the identity (Xbox letters or PlayStation shapes).
struct PadGlyphs: Equatable {
    var symbols: [PadButton: String] = [:]
    var names: [PadButton: String] = [:]

    static let fallback: PadGlyphs = {
        var g = PadGlyphs()
        g.symbols = [.a: "a.circle", .b: "b.circle", .x: "x.circle", .y: "y.circle",
                     .lb: "lb.rectangle.roundedbottom", .rb: "rb.rectangle.roundedbottom",
                     .lt: "lt.rectangle.roundedtop", .rt: "rt.rectangle.roundedtop",
                     .view: "rectangle.on.rectangle.circle", .menu: "line.3.horizontal.circle",
                     .guide: "house.circle", .share: "square.and.arrow.up.circle"]
        g.names = [.a: "A", .b: "B", .x: "X", .y: "Y", .lb: "Left bumper", .rb: "Right bumper",
                   .lt: "Left trigger", .rt: "Right trigger", .ls: "Left stick", .rs: "Right stick",
                   .view: "View", .menu: "Menu", .guide: "Guide", .share: "Share",
                   .up: "Up", .down: "Down", .left: "Left", .right: "Right"]
        return g
    }()

    @MainActor
    init(_ gp: GCExtendedGamepad) {
        self = .fallback
        let map: [(GCControllerElement?, PadButton)] = [
            (gp.buttonA, .a), (gp.buttonB, .b), (gp.buttonX, .x), (gp.buttonY, .y),
            (gp.leftShoulder, .lb), (gp.rightShoulder, .rb),
            (gp.leftTrigger, .lt), (gp.rightTrigger, .rt),
            (gp.buttonOptions, .view), (gp.buttonMenu, .menu), (gp.buttonHome, .guide),
            ((gp as? GCXboxGamepad)?.buttonShare ?? (gp as? GCDualShockGamepad)?.touchpadButton, .share),
        ]
        for (element, button) in map {
            if let symbol = element?.sfSymbolsName { symbols[button] = symbol }
            if let name = element?.localizedName, !name.isEmpty { names[button] = name }
        }
    }

    init() {}
}
