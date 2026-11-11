// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "PadBridge",
    platforms: [.macOS(.v15)],
    targets: [
        .executableTarget(
            name: "PadBridge",
            path: "Sources/PadBridge",
            linkerSettings: [
                .linkedFramework("IOKit"),
                .linkedFramework("IOBluetooth"),
                .linkedFramework("GameController"),
                .linkedFramework("ServiceManagement"),
            ]
        )
    ]
)
