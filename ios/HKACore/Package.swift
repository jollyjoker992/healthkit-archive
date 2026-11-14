// swift-tools-version: 6.0
// HKACore: the protocol side of the healthkit-archive iOS app (see ../../spec/README.md).
// Builds and tests on macOS so the crypto and formats are checked against the spec vectors
// and the reference `age` tool without a phone.
import PackageDescription

let package = Package(
    name: "HKACore",
    platforms: [.iOS(.v17), .macOS(.v14)],
    products: [
        .library(name: "HKACore", targets: ["HKACore"]),
        .executable(name: "hka-swift-device", targets: ["hka-swift-device"]),
    ],
    targets: [
        .target(name: "HKACore", swiftSettings: [.swiftLanguageMode(.v5)]),
        .executableTarget(name: "hka-swift-device", dependencies: ["HKACore"], swiftSettings: [.swiftLanguageMode(.v5)]),
        .testTarget(name: "HKACoreTests", dependencies: ["HKACore"], swiftSettings: [.swiftLanguageMode(.v5)]),
    ]
)
