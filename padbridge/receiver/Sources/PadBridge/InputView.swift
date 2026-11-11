import AppKit
import SwiftUI

/// A schematic of the gamepad laid out like the real thing: triggers and bumpers on top,
/// left stick and face buttons in the upper row, D-pad and right stick below.
struct InputView: View {
    @Environment(BridgeModel.self) private var model

    var body: some View {
        let s = model.input
        let g = model.glyphs
        VStack(spacing: 12) {
            HStack(alignment: .center) {
                TriggerGauge(symbol: g.symbols[.lt], name: g.names[.lt] ?? "Left trigger", value: s.leftTrigger)
                GlyphButton(symbol: g.symbols[.lb], name: g.names[.lb] ?? "Left bumper", pressed: s.pressed.contains(.lb), size: 22)
                Spacer()
                GlyphButton(symbol: g.symbols[.rb], name: g.names[.rb] ?? "Right bumper", pressed: s.pressed.contains(.rb), size: 22)
                TriggerGauge(symbol: g.symbols[.rt], name: g.names[.rt] ?? "Right trigger", value: s.rightTrigger, trailing: true)
            }
            HStack(alignment: .center) {
                StickView(name: g.names[.ls] ?? "Left stick", point: s.leftStick, pressed: s.pressed.contains(.ls))
                Spacer()
                HStack(spacing: 10) {
                    GlyphButton(symbol: g.symbols[.view], name: g.names[.view] ?? "View", pressed: s.pressed.contains(.view), size: 20)
                    GlyphButton(symbol: g.symbols[.guide], name: g.names[.guide] ?? "Guide", pressed: s.pressed.contains(.guide), size: 26)
                    GlyphButton(symbol: g.symbols[.menu], name: g.names[.menu] ?? "Menu", pressed: s.pressed.contains(.menu), size: 20)
                }
                Spacer()
                FaceButtons(state: s, glyphs: g)
            }
            HStack(alignment: .center) {
                DPadView(pressed: s.pressed)
                    .padding(.leading, 64)
                Spacer()
                GlyphButton(symbol: g.symbols[.share], name: g.names[.share] ?? "Share", pressed: s.pressed.contains(.share), size: 18)
                Spacer()
                StickView(name: g.names[.rs] ?? "Right stick", point: s.rightStick, pressed: s.pressed.contains(.rs))
                    .padding(.trailing, 64)
            }
            if !model.hasGamepad {
                Text("Waiting for macOS to report input from the controller…")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 10)
        .frame(maxWidth: 520)
        .frame(maxWidth: .infinity)
    }
}

/// Uses the filled variant of an SF Symbol while pressed, when one exists.
@MainActor
func symbolName(_ base: String, pressed: Bool) -> String {
    guard pressed else { return base }
    let filled = base + ".fill"
    return NSImage(systemSymbolName: filled, accessibilityDescription: nil) != nil ? filled : base
}

struct GlyphButton: View {
    let symbol: String?
    let name: String
    let pressed: Bool
    var size: CGFloat = 24

    var body: some View {
        Group {
            if let symbol {
                Image(systemName: symbolName(symbol, pressed: pressed))
                    .font(.system(size: size, weight: .regular))
            } else {
                Text(name).font(.caption.weight(.semibold))
            }
        }
        .foregroundStyle(pressed ? AnyShapeStyle(.tint) : AnyShapeStyle(.secondary))
        .scaleEffect(pressed ? 0.92 : 1)
        .accessibilityElement()
        .accessibilityLabel(name)
        .accessibilityValue(pressed ? "Pressed" : "Not pressed")
    }
}

struct FaceButtons: View {
    let state: InputState
    let glyphs: PadGlyphs

    var body: some View {
        let spacing: CGFloat = 25
        ZStack {
            button(.y).offset(y: -spacing)
            button(.x).offset(x: -spacing)
            button(.b).offset(x: spacing)
            button(.a).offset(y: spacing)
        }
        .frame(width: 84, height: 84)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Face buttons")
    }

    private func button(_ b: PadButton) -> some View {
        GlyphButton(symbol: glyphs.symbols[b], name: glyphs.names[b] ?? b.rawValue.uppercased(),
                    pressed: state.pressed.contains(b), size: 26)
    }
}

/// Avoids showing "-0.00" for a resting stick.
func tidy(_ v: Double) -> Double { abs(v) < 0.005 ? 0 : v }

struct StickView: View {
    let name: String
    let point: CGPoint
    let pressed: Bool
    private let diameter: CGFloat = 76

    var body: some View {
        VStack(spacing: 6) {
            ZStack {
                Circle()
                    .fill(.quaternary.opacity(0.6))
                Circle()
                    .strokeBorder(.separator, lineWidth: 1)
                Path { p in
                    p.move(to: CGPoint(x: diameter / 2, y: 8)); p.addLine(to: CGPoint(x: diameter / 2, y: diameter - 8))
                    p.move(to: CGPoint(x: 8, y: diameter / 2)); p.addLine(to: CGPoint(x: diameter - 8, y: diameter / 2))
                }
                .stroke(.separator, lineWidth: 0.5)
                Circle()
                    .fill(pressed ? AnyShapeStyle(.tint) : AnyShapeStyle(.primary.opacity(0.75)))
                    .frame(width: 18, height: 18)
                    .overlay { Circle().strokeBorder(.background, lineWidth: 2) }
                    .shadow(color: .black.opacity(0.15), radius: 1.5, y: 1)
                    .offset(x: point.x * (diameter / 2 - 11), y: -point.y * (diameter / 2 - 11))
            }
            .frame(width: diameter, height: diameter)
            Text(String(format: "%+.2f  %+.2f", tidy(point.x), tidy(point.y)))
                .font(.caption2.monospacedDigit())
                .foregroundStyle(.secondary)
        }
        .accessibilityElement()
        .accessibilityLabel(name)
        .accessibilityValue(String(format: "x %.2f, y %.2f%@", point.x, point.y, pressed ? ", pressed" : ""))
    }
}

struct TriggerGauge: View {
    let symbol: String?
    let name: String
    let value: Float
    var trailing = false

    var body: some View {
        HStack(spacing: 8) {
            if trailing { bar; glyph } else { glyph; bar }
        }
        .accessibilityElement()
        .accessibilityLabel(name)
        .accessibilityValue("\(Int(value * 100)) percent")
    }

    private var glyph: some View {
        GlyphButton(symbol: symbol, name: name, pressed: value > 0.5, size: 22)
            .accessibilityHidden(true)
    }

    private var bar: some View {
        VStack(alignment: trailing ? .trailing : .leading, spacing: 3) {
            Capsule()
                .fill(.quaternary)
                .frame(width: 72, height: 6)
                .overlay(alignment: trailing ? .trailing : .leading) {
                    Capsule().fill(.tint).frame(width: max(0, 72 * CGFloat(value)), height: 6)
                }
            Text("\(Int((value * 100).rounded())) %")
                .font(.caption2.monospacedDigit())
                .foregroundStyle(.secondary)
        }
    }
}

struct DPadView: View {
    let pressed: Set<PadButton>
    private let arm: CGFloat = 22
    private let width: CGFloat = 22

    var body: some View {
        ZStack {
            segment(.up).offset(y: -arm)
            segment(.down).offset(y: arm)
            segment(.left).offset(x: -arm)
            segment(.right).offset(x: arm)
            Rectangle().fill(.quaternary).frame(width: width, height: width)
        }
        .frame(width: arm * 2 + width, height: arm * 2 + width)
        .accessibilityElement()
        .accessibilityLabel("D-pad")
        .accessibilityValue(pressedNames.isEmpty ? "Not pressed" : pressedNames)
    }

    private var pressedNames: String {
        [PadButton.up, .down, .left, .right].filter(pressed.contains).map(\.rawValue.capitalized).joined(separator: ", ")
    }

    private func segment(_ b: PadButton) -> some View {
        let on = pressed.contains(b)
        return UnevenRoundedRectangle(cornerRadii: radii(b), style: .continuous)
            .fill(on ? AnyShapeStyle(.tint) : AnyShapeStyle(.quaternary))
            .frame(width: width, height: width)
            .overlay {
                Image(systemName: "arrowtriangle.\(b.rawValue).fill")
                    .font(.system(size: 8))
                    .foregroundStyle(on ? AnyShapeStyle(.white) : AnyShapeStyle(.secondary))
            }
    }

    private func radii(_ b: PadButton) -> RectangleCornerRadii {
        let r: CGFloat = 5
        switch b {
        case .up: return .init(topLeading: r, topTrailing: r)
        case .down: return .init(bottomLeading: r, bottomTrailing: r)
        case .left: return .init(topLeading: r, bottomLeading: r)
        default: return .init(bottomTrailing: r, topTrailing: r)
        }
    }
}
