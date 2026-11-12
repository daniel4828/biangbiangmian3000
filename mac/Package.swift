// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "BiangbiangQuick",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "biangbiang-quick", targets: ["BiangbiangQuick"]),
    ],
    targets: [
        .executableTarget(name: "BiangbiangQuick", path: "Sources/BiangbiangQuick"),
    ]
)
