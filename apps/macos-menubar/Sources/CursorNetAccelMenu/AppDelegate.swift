import AppKit
import Foundation

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private let supervisor = ServiceSupervisor()
    private var statusMenuItem: NSMenuItem!
    private var healthTimer: Timer?
    private var webReachable = false
    /// Mirror of supervisor.state, only touched on main.
    private var mainState: ServiceSupervisor.State = .stopped

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            button.image = menuIcon(running: false)
            button.toolTip = "Cursor 网络加速器"
        }

        let menu = NSMenu()
        let openItem = NSMenuItem(
            title: "打开状态页",
            action: #selector(openStatusPage),
            keyEquivalent: "o"
        )
        openItem.target = self
        menu.addItem(openItem)

        menu.addItem(NSMenuItem.separator())

        statusMenuItem = NSMenuItem(title: "服务：启动中…", action: nil, keyEquivalent: "")
        statusMenuItem.isEnabled = false
        menu.addItem(statusMenuItem)

        let logItem = NSMenuItem(
            title: "打开服务日志",
            action: #selector(openServiceLog),
            keyEquivalent: ""
        )
        logItem.target = self
        menu.addItem(logItem)

        menu.addItem(NSMenuItem.separator())

        let quitItem = NSMenuItem(
            title: "退出",
            action: #selector(quitApp),
            keyEquivalent: "q"
        )
        quitItem.target = self
        menu.addItem(quitItem)

        statusItem.menu = menu

        supervisor.onStateChange = { [weak self] state in
            DispatchQueue.main.async {
                self?.mainState = state
                self?.applySupervisorState(state)
            }
        }
        supervisor.start()

        healthTimer = Timer.scheduledTimer(withTimeInterval: Config.healthProbeInterval, repeats: true) {
            [weak self] _ in
            self?.probeWeb()
        }
        if let healthTimer {
            RunLoop.main.add(healthTimer, forMode: .common)
        }
        probeWeb()
    }

    func applicationWillTerminate(_ notification: Notification) {
        healthTimer?.invalidate()
        supervisor.stopSync()
    }

    // MARK: - Menu actions

    @objc private func openStatusPage() {
        NSWorkspace.shared.open(Config.statusPageURL)
    }

    @objc private func openServiceLog() {
        try? Config.ensureStateDirs()
        let url = Config.serviceLogURL
        if !FileManager.default.fileExists(atPath: url.path) {
            FileManager.default.createFile(atPath: url.path, contents: nil)
        }
        NSWorkspace.shared.open(url)
    }

    @objc private func quitApp() {
        NSApp.terminate(nil)
    }

    // MARK: - UI state

    private func applySupervisorState(_ state: ServiceSupervisor.State) {
        switch state {
        case .stopped:
            statusMenuItem.title = "服务：已停止"
        case .starting:
            statusMenuItem.title = "服务：启动中…"
        case .running(let pid):
            statusMenuItem.title = webReachable
                ? "服务：运行中（pid \(pid)）"
                : "服务：进程在，等待状态页…"
        case .restarting:
            statusMenuItem.title = "服务：重启中…"
        case .failed(let msg):
            statusMenuItem.title = "服务：失败 — \(msg)"
        }
        refreshIcon()
    }

    private func refreshIcon() {
        let running: Bool
        if case .running = mainState, webReachable {
            running = true
        } else {
            running = false
        }
        statusItem.button?.image = menuIcon(running: running)
    }

    private func menuIcon(running: Bool) -> NSImage? {
        // 与 App 图标同源模板，保证闪电方向一致（不用 SF Symbol horizontal）
        let image = Self.loadMenuBarTemplate() ?? NSImage(
            systemSymbolName: running ? "bolt.fill" : "bolt",
            accessibilityDescription: "Cursor 网络加速器"
        )
        image?.isTemplate = true
        statusItem.button?.appearsDisabled = !running
        return image
    }

    private static func loadMenuBarTemplate() -> NSImage? {
        if let resources = Bundle.main.resourceURL,
           let image = templateImage(in: resources) {
            return image
        }
        // 开发态：swift run 时到源码 Resources 找
        let exe = URL(fileURLWithPath: CommandLine.arguments[0]).standardizedFileURL
        var dir = exe.deletingLastPathComponent()
        for _ in 0 ..< 8 {
            if let image = templateImage(in: dir.appendingPathComponent("Resources")) {
                return image
            }
            let parent = dir.deletingLastPathComponent()
            if parent.path == dir.path { break }
            dir = parent
        }
        return nil
    }

    /// 1x/2x/3x 一起装进同一个 NSImage，AppKit 按屏幕倍数挑最清晰的一档。
    /// 只装 1x 时 Retina 会把 18px 直接放大，菜单栏里就是锯齿和糊边。
    private static func templateImage(in directory: URL) -> NSImage? {
        let image = NSImage(size: NSSize(width: 18, height: 18))
        for name in ["MenuBarIcon", "MenuBarIcon@2x", "MenuBarIcon@3x"] {
            let url = directory.appendingPathComponent("\(name).png")
            guard let data = try? Data(contentsOf: url),
                  let rep = NSBitmapImageRep(data: data) else { continue }
            rep.size = NSSize(width: 18, height: 18)
            image.addRepresentation(rep)
        }
        guard !image.representations.isEmpty else { return nil }
        image.isTemplate = true
        return image
    }

    private func probeWeb() {
        var req = URLRequest(url: Config.webBaseURL.appendingPathComponent("api/health"))
        req.timeoutInterval = Config.healthProbeTimeout
        URLSession.shared.dataTask(with: req) { [weak self] _, resp, _ in
            let ok = (resp as? HTTPURLResponse)?.statusCode == 200
            DispatchQueue.main.async {
                guard let self else { return }
                self.webReachable = ok
                self.applySupervisorState(self.mainState)
            }
        }.resume()
    }
}
