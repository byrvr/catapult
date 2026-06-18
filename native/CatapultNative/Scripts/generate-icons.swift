#!/usr/bin/env swift

import AppKit
import Foundation

let scriptURL = URL(fileURLWithPath: CommandLine.arguments[0]).standardizedFileURL
let packageDir = scriptURL.deletingLastPathComponent().deletingLastPathComponent()
let repoRoot = packageDir.deletingLastPathComponent().deletingLastPathComponent()
let fileManager = FileManager.default

func scaled(_ value: CGFloat, _ size: CGFloat) -> CGFloat {
    value * size / 1024
}

func rect(_ x: CGFloat, _ y: CGFloat, _ width: CGFloat, _ height: CGFloat, _ size: CGFloat) -> NSRect {
    NSRect(x: scaled(x, size), y: scaled(y, size), width: scaled(width, size), height: scaled(height, size))
}

func drawArrowAppSymbol(size: CGFloat, color: NSColor, pointSize: CGFloat = 560) {
    guard let symbol = NSImage(systemSymbolName: "arrow.up.forward.app", accessibilityDescription: nil) else {
        return
    }

    let pixelSize = max(1, Int(ceil(scaled(pointSize, size))))
    let configuration = NSImage.SymbolConfiguration(pointSize: CGFloat(pixelSize), weight: .semibold)
    let configured = symbol.withSymbolConfiguration(configuration) ?? symbol
    let symbolRect = rect((1024 - pointSize) / 2, (1024 - pointSize) / 2, pointSize, pointSize, size)
    let target = color.usingColorSpace(.deviceRGB) ?? color

    guard let mask = NSBitmapImageRep(
        bitmapDataPlanes: nil,
        pixelsWide: pixelSize,
        pixelsHigh: pixelSize,
        bitsPerSample: 8,
        samplesPerPixel: 4,
        hasAlpha: true,
        isPlanar: false,
        colorSpaceName: .deviceRGB,
        bytesPerRow: 0,
        bitsPerPixel: 0
    ) else {
        return
    }

    mask.size = NSSize(width: pixelSize, height: pixelSize)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: mask)
    NSGraphicsContext.current?.cgContext.clear(CGRect(x: 0, y: 0, width: pixelSize, height: pixelSize))
    configured.draw(
        in: NSRect(x: 0, y: 0, width: pixelSize, height: pixelSize),
        from: .zero,
        operation: .sourceOver,
        fraction: 1
    )
    NSGraphicsContext.restoreGraphicsState()

    let red = target.redComponent
    let green = target.greenComponent
    let blue = target.blueComponent
    for y in 0..<pixelSize {
        for x in 0..<pixelSize {
            guard let source = mask.colorAt(x: x, y: y), source.alphaComponent > 0 else {
                continue
            }
            mask.setColor(
                NSColor(calibratedRed: red, green: green, blue: blue, alpha: source.alphaComponent),
                atX: x,
                y: y
            )
        }
    }

    let image = NSImage(size: mask.size)
    image.addRepresentation(mask)
    image.draw(in: symbolRect, from: .zero, operation: .sourceOver, fraction: 1)
}

func drawMenuBarSymbol(size: CGFloat) {
    NSColor.black.setStroke()

    let frame = NSBezierPath(
        roundedRect: rect(276, 276, 472, 472, size),
        xRadius: scaled(126, size),
        yRadius: scaled(126, size)
    )
    frame.lineWidth = scaled(66, size)
    frame.lineCapStyle = .round
    frame.lineJoinStyle = .round
    frame.stroke()

    let arrow = NSBezierPath()
    arrow.move(to: NSPoint(x: scaled(410, size), y: scaled(414, size)))
    arrow.line(to: NSPoint(x: scaled(642, size), y: scaled(646, size)))
    arrow.move(to: NSPoint(x: scaled(536, size), y: scaled(650, size)))
    arrow.line(to: NSPoint(x: scaled(646, size), y: scaled(650, size)))
    arrow.line(to: NSPoint(x: scaled(646, size), y: scaled(540, size)))
    arrow.lineWidth = scaled(68, size)
    arrow.lineCapStyle = .round
    arrow.lineJoinStyle = .round
    arrow.stroke()
}

func makeIcon(size: Int, includeBackground: Bool) throws -> NSImage {
    let size = CGFloat(size)
    guard let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil,
        pixelsWide: Int(size),
        pixelsHigh: Int(size),
        bitsPerSample: 8,
        samplesPerPixel: 4,
        hasAlpha: true,
        isPlanar: false,
        colorSpaceName: .deviceRGB,
        bytesPerRow: 0,
        bitsPerPixel: 0
    ) else {
        throw NSError(domain: "CatapultIcon", code: 1, userInfo: [NSLocalizedDescriptionKey: "Unable to create bitmap context"])
    }

    rep.size = NSSize(width: size, height: size)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    let context = NSGraphicsContext.current!.cgContext
    context.setShouldAntialias(true)
    context.setAllowsAntialiasing(true)
    context.clear(CGRect(x: 0, y: 0, width: size, height: size))

    if includeBackground {
        let outer = NSBezierPath(
            roundedRect: rect(72, 72, 880, 880, size),
            xRadius: scaled(210, size),
            yRadius: scaled(210, size)
        )

        NSColor(calibratedRed: 0.25, green: 0.49, blue: 0.96, alpha: 1.0).setFill()
        outer.fill()
        drawArrowAppSymbol(size: size, color: .white)
    } else {
        drawMenuBarSymbol(size: size)
    }

    NSGraphicsContext.restoreGraphicsState()

    let image = NSImage(size: rep.size)
    image.addRepresentation(rep)
    return image
}

func writePNG(_ image: NSImage, to url: URL) throws {
    guard let tiff = image.tiffRepresentation,
          let rep = NSBitmapImageRep(data: tiff),
          let data = rep.representation(using: .png, properties: [:]) else {
        throw NSError(domain: "CatapultIcon", code: 2, userInfo: [NSLocalizedDescriptionKey: "Unable to encode PNG"])
    }
    try data.write(to: url, options: .atomic)
}

func runIconutil(iconsetURL: URL, outputURL: URL) throws {
    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/iconutil")
    process.arguments = ["-c", "icns", iconsetURL.path, "-o", outputURL.path]
    try process.run()
    process.waitUntilExit()
    guard process.terminationStatus == 0 else {
        throw NSError(domain: "CatapultIcon", code: 3, userInfo: [NSLocalizedDescriptionKey: "iconutil failed"])
    }
}

let appIconURL = repoRoot.appendingPathComponent("CatapultIcon.png")
let menuTemplateURL = repoRoot.appendingPathComponent("CatapultMenuBarTemplate.png")
let iconsetURL = repoRoot.appendingPathComponent("Catapult.iconset")
let icnsURL = repoRoot.appendingPathComponent("Catapult.icns")

try? fileManager.removeItem(at: iconsetURL)
try fileManager.createDirectory(at: iconsetURL, withIntermediateDirectories: true)

let entries: [(String, Int)] = [
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
]

for (name, pixelSize) in entries {
    try writePNG(makeIcon(size: pixelSize, includeBackground: true), to: iconsetURL.appendingPathComponent(name))
}

try writePNG(makeIcon(size: 1024, includeBackground: true), to: appIconURL)
try writePNG(makeIcon(size: 64, includeBackground: false), to: menuTemplateURL)
try runIconutil(iconsetURL: iconsetURL, outputURL: icnsURL)
try fileManager.removeItem(at: iconsetURL)

print("Generated \(icnsURL.path)")
