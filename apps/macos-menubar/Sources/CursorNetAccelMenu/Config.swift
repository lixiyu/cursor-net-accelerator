import Foundation

enum Config {
    static let webBaseURL = URL(string: "http://127.0.0.1:8789")!
    static let healthPath = "/api/health"
    static let statusPageURL = webBaseURL.appendingPathComponent("/")
    static let restartBackoff: TimeInterval = 1.5
    static let maxRestartBackoff: TimeInterval = 30
    static let healthProbeInterval: TimeInterval = 8
    static let healthProbeTimeout: TimeInterval = 1.2

    static var stateDir: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/cursor-net-accelerator")
    }

    static var dataDir: URL { stateDir.appendingPathComponent("data") }
    static var menubarLogURL: URL { dataDir.appendingPathComponent("menubar.log") }
    static var serviceLogURL: URL { dataDir.appendingPathComponent("run.log") }
    static var lockURL: URL { stateDir.appendingPathComponent("menubar.lock") }
    static var menubarConfigURL: URL { stateDir.appendingPathComponent("menubar.json") }

    /// Resolve `cursor-net-accelerator` executable.
    static func resolveAcceleratorBinary() -> URL? {
        if let env = ProcessInfo.processInfo.environment["CURSOR_NET_ACCEL_BIN"], !env.isEmpty {
            let u = URL(fileURLWithPath: env)
            if FileManager.default.isExecutableFile(atPath: u.path) { return u }
        }

        if let configured = readConfiguredBin(), FileManager.default.isExecutableFile(atPath: configured.path) {
            return configured
        }

        for root in candidateRepoRoots() {
            let venvBin = root
                .appendingPathComponent(".venv/bin/cursor-net-accelerator")
            if FileManager.default.isExecutableFile(atPath: venvBin.path) {
                return venvBin
            }
        }

        if let pathEnv = ProcessInfo.processInfo.environment["PATH"] {
            for dir in pathEnv.split(separator: ":") {
                let cand = URL(fileURLWithPath: String(dir))
                    .appendingPathComponent("cursor-net-accelerator")
                if FileManager.default.isExecutableFile(atPath: cand.path) {
                    return cand
                }
            }
        }
        return nil
    }

    private static func readConfiguredBin() -> URL? {
        let url = menubarConfigURL
        guard let data = try? Data(contentsOf: url),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let bin = obj["bin"] as? String, !bin.isEmpty
        else { return nil }
        return URL(fileURLWithPath: (bin as NSString).expandingTildeInPath)
    }

    private static func candidateRepoRoots() -> [URL] {
        var roots: [URL] = []
        let fm = FileManager.default
        let cwd = URL(fileURLWithPath: fm.currentDirectoryPath)
        roots.append(cwd)

        let exe = URL(fileURLWithPath: CommandLine.arguments[0]).standardizedFileURL
        roots.append(exe.deletingLastPathComponent())

        // apps/macos-menubar → repo root
        for base in [cwd, exe.deletingLastPathComponent()] {
            var url = base
            for _ in 0 ..< 8 {
                if fm.fileExists(atPath: url.appendingPathComponent("pyproject.toml").path) {
                    roots.append(url)
                }
                let parent = url.deletingLastPathComponent()
                if parent.path == url.path { break }
                url = parent
            }
        }

        // Packaged .app: Contents/MacOS → look beside Applications or Resources hint
        if let res = Bundle.main.resourceURL {
            roots.append(res)
        }

        var seen = Set<String>()
        return roots.filter { seen.insert($0.path).inserted }
    }

    static func ensureStateDirs() throws {
        try FileManager.default.createDirectory(at: dataDir, withIntermediateDirectories: true)
    }
}
