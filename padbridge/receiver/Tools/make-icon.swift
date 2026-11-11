// Draws the PadBridge app icon: a controller crossing a bridge arc on a blue squircle.
// Usage: swift make-icon.swift <output-1024.png>
import AppKit
import CoreGraphics

let size: CGFloat = 1024
let cs = CGColorSpace(name: CGColorSpace.displayP3)!
let ctx = CGContext(data: nil, width: Int(size), height: Int(size), bitsPerComponent: 8, bytesPerRow: 0,
                    space: cs, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
func color(_ r: CGFloat, _ g: CGFloat, _ b: CGFloat, _ a: CGFloat = 1) -> CGColor { CGColor(colorSpace: cs, components: [r, g, b, a])! }

// macOS icon grid: 824 pt body centered in 1024, continuous corners.
let body = CGRect(x: 100, y: 100, width: 824, height: 824)
let squircle = CGPath(roundedRect: body, cornerWidth: 185, cornerHeight: 185, transform: nil)

// Drop shadow
ctx.saveGState()
ctx.setShadow(offset: CGSize(width: 0, height: -12), blur: 28, color: color(0, 0, 0, 0.30))
ctx.addPath(squircle); ctx.setFillColor(color(0.1, 0.2, 0.5)); ctx.fillPath()
ctx.restoreGState()

// Background gradient
ctx.saveGState()
ctx.addPath(squircle); ctx.clip()
let bg = CGGradient(colorsSpace: cs, colors: [color(0.33, 0.62, 1.0), color(0.20, 0.30, 0.86)] as CFArray, locations: [0, 1])!
ctx.drawLinearGradient(bg, start: CGPoint(x: 512, y: 924), end: CGPoint(x: 512, y: 100), options: [])
// soft top highlight
let hl = CGGradient(colorsSpace: cs, colors: [color(1, 1, 1, 0.18), color(1, 1, 1, 0)] as CFArray, locations: [0, 1])!
ctx.drawLinearGradient(hl, start: CGPoint(x: 512, y: 924), end: CGPoint(x: 512, y: 600), options: [])

// Bridge arc behind the controller (signal travelling from laptop to Mac)
ctx.setLineCap(.round)
ctx.setStrokeColor(color(1, 1, 1, 0.38))
ctx.setLineWidth(34)
ctx.addArc(center: CGPoint(x: 512, y: 300), radius: 330, startAngle: .pi * 0.17, endAngle: .pi * 0.83, clockwise: false)
ctx.strokePath()
ctx.setStrokeColor(color(1, 1, 1, 0.22))
ctx.setLineWidth(26)
ctx.addArc(center: CGPoint(x: 512, y: 300), radius: 410, startAngle: .pi * 0.22, endAngle: .pi * 0.78, clockwise: false)
ctx.strokePath()

// Controller silhouette
let pad = CGMutablePath()
pad.addRoundedRect(in: CGRect(x: 250, y: 380, width: 524, height: 230), cornerWidth: 115, cornerHeight: 115)
pad.addEllipse(in: CGRect(x: 232, y: 270, width: 220, height: 260))
pad.addEllipse(in: CGRect(x: 572, y: 270, width: 220, height: 260))
ctx.saveGState()
ctx.setShadow(offset: CGSize(width: 0, height: -10), blur: 24, color: color(0.05, 0.1, 0.35, 0.45))
ctx.addPath(pad); ctx.setFillColor(color(1, 1, 1)); ctx.fillPath()
ctx.restoreGState()

// D-pad and face buttons cut in the background blue
let ink = color(0.24, 0.40, 0.92)
ctx.setFillColor(ink)
let dc = CGPoint(x: 372, y: 478)
ctx.addPath(CGPath(roundedRect: CGRect(x: dc.x - 58, y: dc.y - 19, width: 116, height: 38), cornerWidth: 10, cornerHeight: 10, transform: nil))
ctx.addPath(CGPath(roundedRect: CGRect(x: dc.x - 19, y: dc.y - 58, width: 38, height: 116), cornerWidth: 10, cornerHeight: 10, transform: nil))
ctx.fillPath()
let fc = CGPoint(x: 652, y: 478)
for (dx, dy) in [(0.0, 44.0), (0.0, -44.0), (-44.0, 0.0), (44.0, 0.0)] {
    ctx.addEllipse(in: CGRect(x: fc.x + dx - 21, y: fc.y + dy - 21, width: 42, height: 42))
}
ctx.fillPath()
ctx.restoreGState()

let image = ctx.makeImage()!
let rep = NSBitmapImageRep(cgImage: image)
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
