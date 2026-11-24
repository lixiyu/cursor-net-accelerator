import AppKit
import Foundation

try? Config.ensureStateDirs()

if !SingleInstance.tryAcquire() {
    let alert = NSAlert()
    alert.messageText = "Cursor 网络加速器已在运行"
    alert.informativeText = "菜单栏里已有实例，不必重复打开。"
    alert.alertStyle = .informational
    alert.addButton(withTitle: "好")
    // Need a tiny app runloop for alert when accessory
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    alert.runModal()
    exit(0)
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
