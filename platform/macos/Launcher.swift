import AppKit
import Foundation

// The helper remains responsive while the native game owns its own window.
@MainActor
final class Launcher: NSObject, NSApplicationDelegate {
    private var child: Process?
    private var quitting = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        let bundle = Bundle.main
        guard let gamePath = bundle.object(forInfoDictionaryKey: "FFRGameDataPath") as? String else {
            showError("缺少游戏资源路径，请重新构建应用。")
            NSApp.terminate(nil)
            return
        }
        let process = Process()
        process.executableURL = bundle.executableURL!.deletingLastPathComponent().appendingPathComponent("nxport")
        process.arguments = ["run", gamePath]
        let failure = Pipe()
        process.standardError = failure
        child = process
        do {
            try process.run()
            DispatchQueue.global(qos: .utility).async {
                let data = failure.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                DispatchQueue.main.async {
                    self.finished(process.terminationStatus, detail: String(data: data, encoding: .utf8) ?? "")
                }
            }
        } catch {
            showError(error.localizedDescription)
            NSApp.terminate(nil)
        }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let child, child.isRunning else { return .terminateNow }
        if !quitting {
            quitting = true
            child.terminate() // The native host handles SIGTERM as a graceful exit.
        }
        return .terminateLater
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        if let child, child.isRunning {
            NSRunningApplication(processIdentifier: child.processIdentifier)?.activate(options: [])
        }
        return false
    }

    private func finished(_ status: Int32, detail: String) {
        if quitting {
            NSApp.reply(toApplicationShouldTerminate: true)
            return
        }
        if status != 0 { showError(detail.isEmpty ? "游戏已中止，请查看运行日志。" : detail) }
        NSApp.terminate(nil)
    }

    private func showError(_ detail: String) {
        NSApp.activate()
        let alert = NSAlert()
        alert.messageText = "FINAL FANTASY RESONANCE 启动失败"
        alert.informativeText = detail
        alert.addButton(withTitle: "好")
        alert.runModal()
    }
}

MainActor.assumeIsolated {
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    let launcher = Launcher()
    app.delegate = launcher
    withExtendedLifetime(launcher) { app.run() }
}
