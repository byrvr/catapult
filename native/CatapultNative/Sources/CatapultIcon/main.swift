import AppKit
import Foundation

// catapult-icon <Assets.car> <icon-name> <out.png>
//
// Pulls one icon out of a compiled asset catalog and writes it as a PNG. The
// exit codes are the contract with the backend's icon extraction.

private enum ExitCode: Int32 {
    case success = 0
    case noIcon = 1
    case badArguments = 2
    case catalogUnavailable = 3
}

// CoreUI is the private framework macOS itself reads .car files with, so it
// understands every catalog format the OS does. It is loaded at runtime and
// called through C function pointers because its selectors carry integer and
// CGFloat arguments that `perform(_:)` cannot pass.
private let coreUIPath = "/System/Library/PrivateFrameworks/CoreUI.framework"

// Argument types mirror the selectors' runtime encodings exactly: deviceSubtype
// is unsigned (Q) and desiredSize is a CGSize struct, which travels in
// floating-point registers. Declaring it as an integer compiles, but the
// callee then reads whatever those registers held.
private typealias IconImageFn = @convention(c) (
    AnyObject, Selector, NSString, CGFloat, Int, UInt, Int, Int, Int, Int, CGSize
) -> AnyObject?
private typealias ImageFn = @convention(c) (
    AnyObject, Selector, NSString, CGFloat, Int, UInt, Int, Int, Int, Int
) -> AnyObject?
private typealias CGImageFn = @convention(c) (AnyObject, Selector) -> UnsafeRawPointer?

// The marketing idiom (6) carries the 1024px App Store rendition; the rest are
// phone, pad, tv and universal, in the order a real device icon is preferred.
private let deviceIdioms = [6, 1, 2, 3, 0]
private let desiredSizes = [1024, 512, 256, 180, 120, 60]
private let scaleFactors: [CGFloat] = [3, 2, 1]

private func openCatalog(at url: URL) -> NSObject? {
    guard let bundle = Bundle(path: coreUIPath), bundle.load(),
          let catalogClass = NSClassFromString("CUICatalog") else {
        return nil
    }
    let alloc = NSSelectorFromString("alloc")
    // alloc and init hand back owned references, so take them retained.
    guard let allocated = (catalogClass as AnyObject).perform(alloc)?.takeRetainedValue() as? NSObject else {
        return nil
    }
    let initialize = NSSelectorFromString("initWithURL:error:")
    return allocated.perform(initialize, with: url, with: nil)?.takeRetainedValue() as? NSObject
}

/// The CGImage behind a CUINamedImage. Its `image` getter returns a bare
/// CGImageRef, which key-value coding would try to box as an object.
private func cgImage(of named: AnyObject?) -> CGImage? {
    guard let named = named as? NSObject else { return nil }
    let selector = NSSelectorFromString("image")
    guard named.responds(to: selector), let method = named.method(for: selector),
          let raw = unsafeBitCast(method, to: CGImageFn.self)(named, selector) else {
        return nil
    }
    return Unmanaged<CGImage>.fromOpaque(raw).takeUnretainedValue()
}

private func largest(_ images: [CGImage]) -> CGImage? {
    images.max { $0.width < $1.width }
}

/// Renditions of an "Icon Image" asset, the kind Xcode compiles from an app
/// icon set. The plain image lookups return nil for these.
private func iconImages(in catalog: NSObject, named name: NSString) -> [CGImage] {
    let selector = NSSelectorFromString(
        "iconImageWithName:scaleFactor:deviceIdiom:deviceSubtype:displayGamut:"
            + "layoutDirection:sizeClassHorizontal:sizeClassVertical:desiredSize:"
    )
    guard catalog.responds(to: selector), let method = catalog.method(for: selector) else { return [] }
    let lookup = unsafeBitCast(method, to: IconImageFn.self)
    var images: [CGImage] = []
    for idiom in deviceIdioms {
        for size in desiredSizes {
            let desired = CGSize(width: CGFloat(size), height: CGFloat(size))
            if let image = cgImage(of: lookup(catalog, selector, name, 1, idiom, 0, 0, 0, 0, 0, desired)) {
                images.append(image)
            }
        }
    }
    return images
}

/// Renditions of an ordinary image set, for apps whose icon is not compiled as
/// an icon asset.
private func imageSetImages(in catalog: NSObject, named name: NSString) -> [CGImage] {
    let selector = NSSelectorFromString(
        "imageWithName:scaleFactor:deviceIdiom:deviceSubtype:displayGamut:"
            + "layoutDirection:sizeClassHorizontal:sizeClassVertical:"
    )
    guard catalog.responds(to: selector), let method = catalog.method(for: selector) else { return [] }
    let lookup = unsafeBitCast(method, to: ImageFn.self)
    var images: [CGImage] = []
    for idiom in deviceIdioms {
        for scale in scaleFactors {
            if let image = cgImage(of: lookup(catalog, selector, name, scale, idiom, 0, 0, 0, 0, 0)) {
                images.append(image)
            }
        }
    }
    return images
}

private func writePNG(_ image: CGImage, to url: URL) -> Bool {
    guard let png = NSBitmapImageRep(cgImage: image).representation(using: .png, properties: [:]) else {
        return false
    }
    do {
        try png.write(to: url)
        return true
    } catch {
        return false
    }
}

private func fail(_ message: String, _ code: ExitCode) -> ExitCode {
    FileHandle.standardError.write(Data("catapult-icon: \(message)\n".utf8))
    return code
}

private func run(_ arguments: [String]) -> ExitCode {
    guard arguments.count == 4 else {
        return fail("usage: catapult-icon <Assets.car> <icon-name> <out.png>", .badArguments)
    }
    guard let catalog = openCatalog(at: URL(fileURLWithPath: arguments[1])) else {
        return fail("could not open \(arguments[1])", .catalogUnavailable)
    }
    let name = arguments[2] as NSString
    guard let image = largest(iconImages(in: catalog, named: name))
            ?? largest(imageSetImages(in: catalog, named: name)) else {
        return fail("no icon named \(name)", .noIcon)
    }
    guard writePNG(image, to: URL(fileURLWithPath: arguments[3])) else {
        return fail("could not write \(arguments[3])", .badArguments)
    }
    return .success
}

exit(run(CommandLine.arguments).rawValue)
