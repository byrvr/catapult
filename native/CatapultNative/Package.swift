// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "CatapultNative",
    platforms: [
        .macOS(.v14)
    ],
    products: [
        .executable(name: "CatapultNative", targets: ["CatapultNative"])
    ],
    targets: [
        .executableTarget(
            name: "CatapultNative"
        )
    ]
)
