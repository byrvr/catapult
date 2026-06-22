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
let renderScale: CGFloat = 1
let canvas = NSSize(width: CGFloat(width), height: CGFloat(height))

func scaled(_ value: CGFloat) -> CGFloat {
    value
}

func rect(_ x: CGFloat, _ y: CGFloat, _ w: CGFloat, _ h: CGFloat) -> NSRect {
    NSRect(x: x, y: y, width: w, height: h)
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
    pixelsWide: Int(canvas.width * renderScale),
    pixelsHigh: Int(canvas.height * renderScale),
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

rep.size = NSSize(width: CGFloat(width), height: CGFloat(height))
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
let context = NSGraphicsContext.current!.cgContext
context.setShouldAntialias(true)

let background = NSBezierPath(rect: rect(0, 0, CGFloat(width), CGFloat(height)))
NSGradient(colors: [
    color(0.96, 0.975, 1.0),
    color(0.90, 0.93, 0.97),
])?.draw(in: background, angle: -35)

let topSheen = NSBezierPath(ovalIn: rect(180, 256, 400, 170))
NSGradient(colors: [
    color(1, 1, 1, 0.78),
    color(1, 1, 1, 0.0),
])?.draw(in: topSheen, relativeCenterPosition: NSPoint(x: 0, y: 0))

let accent = NSBezierPath(ovalIn: rect(246, 126, 268, 140))
NSGradient(colors: [
    color(0.14, 0.47, 1.0, 0.14),
    color(0.14, 0.47, 1.0, 0.0),
])?.draw(in: accent, relativeCenterPosition: NSPoint(x: 0, y: 0))

drawText(
    "Install Catapult",
    x: 40,
    y: 344,
    size: 24,
    weight: .semibold,
    color: color(0.10, 0.14, 0.20)
)
drawText(
    "Drag Catapult into Applications.",
    x: 40,
    y: 314,
    size: 13,
    weight: .medium,
    color: color(0.37, 0.43, 0.52)
)

let arrow = NSBezierPath()
arrow.move(to: NSPoint(x: scaled(304), y: scaled(204)))
arrow.line(to: NSPoint(x: scaled(456), y: scaled(204)))
arrow.lineWidth = scaled(3)
arrow.lineCapStyle = .round
color(0.12, 0.45, 0.92, 0.54).setStroke()
arrow.stroke()

let arrowHead = NSBezierPath()
arrowHead.move(to: NSPoint(x: scaled(474), y: scaled(204)))
arrowHead.line(to: NSPoint(x: scaled(450), y: scaled(219)))
arrowHead.line(to: NSPoint(x: scaled(450), y: scaled(189)))
arrowHead.close()
color(0.12, 0.45, 0.92, 0.62).setFill()
arrowHead.fill()

let leftPlate = NSBezierPath(roundedRect: rect(118, 118, 180, 172), xRadius: scaled(24), yRadius: scaled(24))
color(1, 1, 1, 0.64).setFill()
leftPlate.fill()
color(0.34, 0.43, 0.56, 0.12).setStroke()
leftPlate.lineWidth = scaled(1)
leftPlate.stroke()

let rightPlate = NSBezierPath(roundedRect: rect(462, 118, 180, 172), xRadius: scaled(24), yRadius: scaled(24))
color(1, 1, 1, 0.64).setFill()
rightPlate.fill()
color(0.34, 0.43, 0.56, 0.12).setStroke()
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
