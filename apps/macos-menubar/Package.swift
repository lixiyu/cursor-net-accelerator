// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "CursorNetAccelMenu",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "CursorNetAccelMenu",
            path: "Sources/CursorNetAccelMenu"
        ),
    ]
)
