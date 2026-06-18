#!/usr/bin/env swift

import AppKit
import Foundation

let scriptURL = URL(fileURLWithPath: CommandLine.arguments[0]).standardizedFileURL
let packageDir = scriptURL.deletingLastPathComponent().deletingLastPathComponent()
let repoRoot = packageDir.deletingLastPathComponent().deletingLastPathComponent()
let outputURL = repoRoot.appendingPathComponent("dist/dmg-background.png")
let fileManager = FileManager.default

try fileManager.createDirectory(
    at: outputURL.deletingLastPathComponent(),
    withIntermediateDirectories: true
)

let width = 760
let height = 430
let scale: CGFloat = 1
let canvas = NSSize(width: CGFloat(width), height: CGFloat(height))

func scaled(_ value: CGFloat) -> CGFloat {
    value * scale
}

func rect(_ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat) -> NSRect {
    NSRect(x: scaled(x), y: scaled(y), width: scaled(w), height: scaled(h))
}

func color(_ red: CGFloat, _ green: CGFloat, _ blue: CGFloat, _ alpha: CGFloat = 1) -> NSColor {
    NSColor(calibratedRed: red, green: green, blue: blue, alpha: alpha)
}

func drawText(_ string: String, x: CGFloat, y: CGFloat, width: CGFloat? = nil, size: CGFloat, weight: NSFont.Weight, color: NSColor, alignment: NSTextAlignment = .center) {
    let paragraph = NSMutableParagraphStyle()
    paragraph.alignment = alignment
    let attrs: [NSAttributedString.Key: Any] = [
        .font: NSFont.systemFont(ofSize: scaled(size), weight: weight),
        .foregroundColor: color,
        .paragraphStyle: paragraph,
    ]
    string.draw(with: rect(x, y, width ?? 760 - (x * 2), size + 12), options: [.usesLineFragmentOrigin], attributes: attrs)
}

guard let rep = NSBitmapImageRep(
    bitmapDataPlanes: nil,
    pixelsWide: Int(canvas.width),
    pixelsHigh: Int(canvas.height),
    bitsPerSample: 8,
    samplesPerPixel: 4,
    hasAlpha: true,
    isPlanar: false,
    colorSpaceName: .deviceRGB,
    bytesPerRow: 0,
    bitsPerPixel: 0
) else {
    fatalError("Unable to create bitmap")
}

rep.size = NSSize(width: width, height: height)
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
let context = NSGraphicsContext.current!.cgContext
context.setShouldAntialias(true)

let background = NSBezierPath(rect: rect(0, 0, CGFloat(width), CGFloat(height)))
NSGradient(colors: [
    color(0.09, 0.11, 0.12),
    color(0.11, 0.13, 0.13),
])?.draw(in: background, angle: -35)

let glow = NSBezierPath(ovalIn: rect(248, 112, 264, 206))
NSGradient(colors: [
    color(0.12, 0.46, 1.0, 0.24),
    color(0.12, 0.46, 1.0, 0.0),
])?.draw(in: glow, relativeCenterPosition: NSPoint(x: 0, y: 0))

drawText(
    "Install Catapult",
    x: 40,
    y: 350,
    size: 23,
    weight: .semibold,
    color: color(0.93, 0.94, 0.95)
)
drawText(
    "Drag the app into Applications.",
    x: 40,
    y: 322,
    size: 13,
    weight: .medium,
    color: color(0.64, 0.67, 0.68)
)

let arrow = NSBezierPath()
arrow.move(to: NSPoint(x: scaled(290), y: scaled(205)))
arrow.line(to: NSPoint(x: scaled(468), y: scaled(205)))
arrow.lineWidth = scaled(4)
arrow.lineCapStyle = .round
color(0.30, 0.56, 1.0, 0.74).setStroke()
arrow.stroke()

let arrowHead = NSBezierPath()
arrowHead.move(to: NSPoint(x: scaled(492), y: scaled(205)))
arrowHead.line(to: NSPoint(x: scaled(458), y: scaled(224)))
arrowHead.line(to: NSPoint(x: scaled(458), y: scaled(186)))
arrowHead.close()
color(0.30, 0.56, 1.0, 0.82).setFill()
arrowHead.fill()

let leftPlate = NSBezierPath(roundedRect: rect(122, 122, 172, 150), xRadius: scaled(24), yRadius: scaled(24))
color(1, 1, 1, 0.035).setFill()
leftPlate.fill()
color(1, 1, 1, 0.07).setStroke()
leftPlate.lineWidth = scaled(1)
leftPlate.stroke()

let rightPlate = NSBezierPath(roundedRect: rect(466, 122, 172, 150), xRadius: scaled(24), yRadius: scaled(24))
color(1, 1, 1, 0.035).setFill()
rightPlate.fill()
color(1, 1, 1, 0.07).setStroke()
rightPlate.lineWidth = scaled(1)
rightPlate.stroke()

NSGraphicsContext.restoreGraphicsState()

let image = NSImage(size: rep.size)
image.addRepresentation(rep)
guard let tiff = image.tiffRepresentation,
      let bitmap = NSBitmapImageRep(data: tiff),
      let data = bitmap.representation(using: .png, properties: [:]) else {
    fatalError("Unable to encode PNG")
}

try data.write(to: outputURL, options: .atomic)
print(outputURL.path)
