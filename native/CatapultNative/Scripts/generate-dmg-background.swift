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
let height = 500
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
    color(0.985, 0.988, 0.993),
    color(0.925, 0.945, 0.970),
])?.draw(in: background, angle: -20)

let subtleTop = NSBezierPath(rect: rect(0, 350, CGFloat(width), 150))
NSGradient(colors: [
    color(1, 1, 1, 0.68),
    color(1, 1, 1, 0.0),
])?.draw(in: subtleTop, angle: -90)

let arrow = NSBezierPath()
arrow.move(to: NSPoint(x: scaled(300), y: scaled(214)))
arrow.line(to: NSPoint(x: scaled(460), y: scaled(214)))
arrow.lineWidth = scaled(2)
arrow.lineCapStyle = .round
color(0.08, 0.36, 0.80, 0.22).setStroke()
arrow.stroke()

let arrowHead = NSBezierPath()
arrowHead.move(to: NSPoint(x: scaled(476), y: scaled(214)))
arrowHead.line(to: NSPoint(x: scaled(454), y: scaled(228)))
arrowHead.line(to: NSPoint(x: scaled(454), y: scaled(200)))
arrowHead.close()
color(0.08, 0.36, 0.80, 0.28).setFill()
arrowHead.fill()

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
