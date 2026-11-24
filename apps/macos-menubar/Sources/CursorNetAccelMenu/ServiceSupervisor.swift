import Foundation
import os.log

/// Owns the accelerator child process: start, in-app keep-alive, clean stop.
/// Parent death → wrapper notices PPID gone → kills child (no orphan).
final class ServiceSupervisor {
    enum State: Equatable {
        case stopped
        case starting
        case running(pid: Int32)
        case restarting
        case failed(String)
    }

    private let log = OSLog(subsystem: "cursor-net-accelerator", category: "menubar")
    private let queue = DispatchQueue(label: "cursor-net-accel.supervisor")

    private var wrapper: Process?
    private var intentionalStop = false
    private var restartAttempt = 0
    private var restartWorkItem: DispatchWorkItem?
    private var binaryURL: URL?

    private(set) var state: State = .stopped {
        didSet { onStateChange?(state) }
    }

    var onStateChange: ((State) -> Void)?

    func start() {
        queue.async { [weak self] in
            self?._start(resetBackoff: true)
        }
    }

    /// Graceful shutdown: stop keep-alive, run CLI `stop` (neutralize DNS), reap wrapper.
    func stopSync() {
        let sem = DispatchSemaphore(value: 0)
        queue.async { [weak self] in
            defer { sem.signal() }
            self?._stop()
        }
        _ = sem.wait(timeout: .now() + 15)
    }

    private func _start(resetBackoff: Bool) {
        if resetBackoff {
            restartAttempt = 0
            intentionalStop = false
        }
        guard !intentionalStop else { return }

        if case .running = state { return }
        if case .starting = state { return }

        guard let bin = Config.resolveAcceleratorBinary() else {
            state = .failed("找不到 cursor-net-accelerator，请设置 CURSOR_NET_ACCEL_BIN 或 menubar.json")
            return
        }
        binaryURL = bin

        do {
            try Config.ensureStateDirs()
        } catch {
            state = .failed("无法创建状态目录: \(error.localizedDescription)")
            return
        }

        state = .starting

        // Take over: clear any leftover CLI/`run` so we own the only instance.
        let stopPre = Process()
        stopPre.executableURL = bin
        stopPre.arguments = ["stop"]
        stopPre.standardOutput = FileHandle.nullDevice
        stopPre.standardError = FileHandle.nullDevice
        try? stopPre.run()
        stopPre.waitUntilExit()
        // Brief pause so UDP/TCP ports release
        Thread.sleep(forTimeInterval: 0.4)

        let parentPID = ProcessInfo.processInfo.processIdentifier
        let logPath = Config.serviceLogURL.path
        // No `set -e`: kill/wait may return non-zero; we still must reap.
        let wrapperScript = """
        BIN="$1"
        PARENT="$2"
        LOG="$3"
        shift 3
        "$BIN" "$@" >>"$LOG" 2>&1 &
        CHILD=$!
        echo "wrapper: parent=$PARENT child=$CHILD bin=$BIN" >>"$LOG"
        while kill -0 "$PARENT" 2>/dev/null; do
          if ! kill -0 "$CHILD" 2>/dev/null; then
            wait "$CHILD" || true
            exit 1
          fi
          sleep 1
        done
        echo "wrapper: parent gone, stopping child=$CHILD" >>"$LOG"
        kill -TERM "$CHILD" 2>/dev/null || true
        for i in 1 2 3 4 5 6 7 8 9 10; do
          kill -0 "$CHILD" 2>/dev/null || break
          sleep 0.3
        done
        kill -KILL "$CHILD" 2>/dev/null || true
        wait "$CHILD" 2>/dev/null || true
        "$BIN" stop >>"$LOG" 2>&1 || true
        exit 0
        """

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        // bash -c: $0=name, $1=bin, $2=parent, $3=log, $4+=args to accelerator
        process.arguments = [
            "-c", wrapperScript,
            "accel-wrapper",
            bin.path,
            String(parentPID),
            logPath,
            "run",
        ]
        // .venv/bin/foo → repo root (best-effort cwd)
        var cwd = bin.deletingLastPathComponent() // bin
        cwd = cwd.deletingLastPathComponent() // .venv
        cwd = cwd.deletingLastPathComponent() // repo
        process.currentDirectoryURL = cwd

        // Inherit useful env; force unbuffered-ish logs via PYTHONUNBUFFERED
        var env = ProcessInfo.processInfo.environment
        env["PYTHONUNBUFFERED"] = "1"
        env["CURSOR_NET_ACCEL_LOG"] = logPath
        process.environment = env

        process.terminationHandler = { [weak self] proc in
            self?.queue.async {
                self?.onWrapperExited(status: proc.terminationStatus)
            }
        }

        do {
            // Truncate / append header
            if !FileManager.default.fileExists(atPath: logPath) {
                FileManager.default.createFile(atPath: logPath, contents: nil)
            }
            let header = "\n===== menubar spawn \(ISO8601DateFormatter().string(from: Date())) =====\n"
            if let handle = try? FileHandle(forWritingTo: Config.serviceLogURL) {
                defer { try? handle.close() }
                try handle.seekToEnd()
                if let data = header.data(using: .utf8) {
                    try handle.write(contentsOf: data)
                }
            }

            try process.run()
            wrapper = process
            state = .running(pid: process.processIdentifier)
            os_log("started wrapper pid=%{public}d bin=%{public}@", log: log, type: .info,
                   process.processIdentifier, bin.path)
        } catch {
            state = .failed("启动失败: \(error.localizedDescription)")
            scheduleRestart()
        }
    }

    private func onWrapperExited(status: Int32) {
        wrapper = nil
        if intentionalStop {
            state = .stopped
            return
        }
        os_log("wrapper exited status=%{public}d, will restart", log: log, type: .default, status)
        state = .restarting
        scheduleRestart()
    }

    private func scheduleRestart() {
        restartWorkItem?.cancel()
        let attempt = restartAttempt
        restartAttempt += 1
        let delay = min(
            Config.restartBackoff * pow(1.6, Double(attempt)),
            Config.maxRestartBackoff
        )
        let work = DispatchWorkItem { [weak self] in
            self?._start(resetBackoff: false)
        }
        restartWorkItem = work
        queue.asyncAfter(deadline: .now() + delay, execute: work)
    }

    private func _stop() {
        intentionalStop = true
        restartWorkItem?.cancel()
        restartWorkItem = nil

        // Prefer CLI stop: kills run + neutralizes resolver
        if let bin = binaryURL ?? Config.resolveAcceleratorBinary() {
            let stop = Process()
            stop.executableURL = bin
            stop.arguments = ["stop"]
            stop.standardOutput = FileHandle.nullDevice
            stop.standardError = FileHandle.nullDevice
            try? stop.run()
            stop.waitUntilExit()
        }

        if let wrapper, wrapper.isRunning {
            wrapper.terminate()
            let deadline = Date().addingTimeInterval(5)
            while wrapper.isRunning, Date() < deadline {
                Thread.sleep(forTimeInterval: 0.1)
            }
            if wrapper.isRunning {
                wrapper.interrupt()
            }
        }
        wrapper = nil
        state = .stopped
    }
}
