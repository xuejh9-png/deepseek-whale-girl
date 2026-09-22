// WorkBuddy Pet —— 桌面宠物宿主
//
// 一个透明、无边框、可置顶、可拖动的小窗口，里面装着同一只鲸鱼娘。
// 窗口比角色略大，但角色之外的区域会动态「穿透」鼠标 —— 不挡你操作别的东西。
//
// 编译见 desktop/build.sh；启动见 启动桌面宠物.command。

import Cocoa
import WebKit

let DEFAULT_URL = "http://127.0.0.1:8791/"

// MARK: - 网页 → 原生 的指令桥

final class Bridge: NSObject, WKScriptMessageHandler {
    weak var host: AppDelegate?

    func userContentController(_ uc: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        guard message.name == "petHost",
              let body = message.body as? [String: Any],
              let cmd = body["cmd"] as? String else { return }
        DispatchQueue.main.async { [weak self] in
            self?.host?.handle(cmd: cmd, body: body)
        }
    }
}

// MARK: - 应用

final class AppDelegate: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    var webView: WKWebView!
    let bridge = Bridge()

    var baseURL = DEFAULT_URL
    var dragging = false
    var dragStartMouse = NSPoint.zero
    var dragStartWin = NSPoint.zero
    var panelOpen = false
    var pinned = true
    var moveMonitor: Any?

    let winW: CGFloat = 280
    let winH: CGFloat = 430

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)      // 不占 Dock
        bridge.host = self
        buildWindow()
        loadPage()
        watchMouse()
    }

    // MARK: 窗口

    func buildWindow() {
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: winW, height: winH),
                          styleMask: [.borderless],
                          backing: .buffered,
                          defer: false)
        window.isOpaque = false                    // 透明
        window.backgroundColor = .clear
        window.hasShadow = false
        window.level = .floating                   // 置顶
        window.isReleasedWhenClosed = false
        window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]

        if let screen = NSScreen.main {
            let f = screen.visibleFrame
            window.setFrameOrigin(NSPoint(x: f.maxX - winW - 20, y: f.minY + 20))
        }

        let cfg = WKWebViewConfiguration()
        cfg.userContentController.add(bridge, name: "petHost")

        webView = WKWebView(frame: NSRect(x: 0, y: 0, width: winW, height: winH),
                            configuration: cfg)
        webView.setValue(false, forKey: "drawsBackground")   // WebView 自身也透明
        if #available(macOS 12.0, *) {
            webView.underPageBackgroundColor = .clear
        }
        window.contentView = webView
        window.makeKeyAndOrderFront(nil)
    }

    func loadPage() {
        let sep = baseURL.contains("?") ? "&" : "?"
        guard let url = URL(string: baseURL + sep + "desktop=1") else { return }
        webView.load(URLRequest(url: url))
    }

    // MARK: 鼠标穿透 —— 只有角色所在的区域吃鼠标事件

    func watchMouse() {
        moveMonitor = NSEvent.addGlobalMonitorForEvents(matching: [.mouseMoved]) { [weak self] _ in
            self?.updatePassthrough()
        }
    }

    func updatePassthrough() {
        guard !dragging else { return }
        if panelOpen {                              // 面板打开时整窗可点
            window.ignoresMouseEvents = false
            return
        }
        let mouse = NSEvent.mouseLocation
        let f = window.frame
        // 角色大致占据窗口的中下部
        let hit = NSRect(x: f.minX + f.width * 0.14,
                         y: f.minY + f.height * 0.035,
                         width: f.width * 0.72,
                         height: f.height * 0.66)
        window.ignoresMouseEvents = !hit.contains(mouse)
    }

    // MARK: 处理网页指令

    func handle(cmd: String, body: [String: Any]) {
        switch cmd {
        case "dragBegin":
            dragging = true
            dragStartMouse = NSEvent.mouseLocation
            dragStartWin = window.frame.origin

        case "dragMove":
            let m = NSEvent.mouseLocation
            window.setFrameOrigin(NSPoint(x: dragStartWin.x + (m.x - dragStartMouse.x),
                                          y: dragStartWin.y + (m.y - dragStartMouse.y)))

        case "dragEnd":
            dragging = false
            updatePassthrough()

        case "panel":
            panelOpen = (body["open"] as? Bool) ?? false
            updatePassthrough()

        case "menu":
            dragging = false
            showMenu()

        default:
            break
        }
    }

    // MARK: 右键菜单（无边框窗口没有关闭按钮，退出走这里）

    func showMenu() {
        let menu = NSMenu()
        let pin = NSMenuItem(title: pinned ? "取消置顶" : "置顶显示",
                             action: #selector(togglePin), keyEquivalent: "")
        pin.target = self
        menu.addItem(pin)
        menu.addItem(NSMenuItem.separator())
        let quit = NSMenuItem(title: "退出桌面宠物",
                              action: #selector(quitApp), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)
        menu.popUp(positioning: nil, at: NSEvent.mouseLocation, in: nil)
    }

    @objc func togglePin() {
        pinned.toggle()
        window.level = pinned ? .floating : .normal
    }

    @objc func quitApp() {
        NSApp.terminate(nil)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return true
    }
}

// MARK: - 入口

let app = NSApplication.shared
let delegate = AppDelegate()

let argv = CommandLine.arguments
if let i = argv.firstIndex(of: "--url"), i + 1 < argv.count {
    delegate.baseURL = argv[i + 1]
}

app.delegate = delegate
app.run()
