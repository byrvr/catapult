// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "CatapultNative",
    platforms: [
        .macOS(.v14)
    ],
    products: [
        .executable(name: "CatapultNative", targets: ["CatapultNative"]),
        // A second product means a bare `swift run` is ambiguous; the docs say
        // `swift run CatapultNative`. Without the product SwiftPM would not
        // build the helper at all.
        .executable(name: "catapult-icon", targets: ["CatapultIcon"])
    ],
    targets: [
        .executableTarget(
            name: "CatapultNative"
        ),
        .executableTarget(
            name: "CatapultIcon"
        )
    ]
)
