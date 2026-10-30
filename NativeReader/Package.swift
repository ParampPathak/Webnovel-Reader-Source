// swift-tools-version: 6.0
import PackageDescription
let package = Package(name: "ReaderCore", platforms: [.macOS(.v13), .iOS(.v17)], products: [.library(name: "ReaderCore", targets: ["ReaderCore"])], targets: [.target(name: "ReaderCore"), .testTarget(name: "ReaderCoreTests", dependencies: ["ReaderCore"])])
