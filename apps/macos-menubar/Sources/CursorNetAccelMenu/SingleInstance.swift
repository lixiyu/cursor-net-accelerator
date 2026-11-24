import Foundation

/// File-lock based single instance. Lock is released when process exits (including crash).
enum SingleInstance {
    private static var lockFD: Int32 = -1

    static func tryAcquire() -> Bool {
        let path = Config.lockURL.path
        try? Config.ensureStateDirs()
        let fd = open(path, O_CREAT | O_RDWR, 0o644)
        guard fd >= 0 else { return false }
        if flock(fd, LOCK_EX | LOCK_NB) != 0 {
            close(fd)
            return false
        }
        // Record pid for humans reading the lock file
        let pid = "\(ProcessInfo.processInfo.processIdentifier)\n"
        pid.withCString { ptr in
            ftruncate(fd, 0)
            lseek(fd, 0, SEEK_SET)
            write(fd, ptr, strlen(ptr))
        }
        lockFD = fd
        return true
    }
}
