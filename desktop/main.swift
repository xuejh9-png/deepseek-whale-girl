// WorkBuddy Pet —— 桌面宠物宿主
//
// 一个透明、无边框、可置顶、可拖动的小窗口，里面装着同一只鲸鱼娘。
// 窗口比角色略大，但角色之外的区域会动态「穿透」鼠标 —— 不挡你操作别的东西。
//
// 编译见 desktop/build.sh；启动见 启动桌面宠物.command。

import Cocoa
import WebKit
import Carbon.HIToolbox

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
    var pinned = true
    var moveMonitor: Any?
    var quitHotKey: EventHotKeyRef?
    var quitHotKeyHandler: EventHandlerRef?

    // 窗口 = 素材画布的实际显示尺寸（320×400 画布 @ 0.5 缩放）。
    // 角色本体占画布高度 64%，对应屏幕高度 128 CSS px。
    // 改大小：同时改 web/pet.css 的 --pet-char-height 和这两个值。
    let winW: CGFloat = 160
    let winH: CGFloat = 200

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)      // 不占 Dock
        bridge.host = self
        buildWindow()
        loadPage()
        watchMouse()
        installQuitHotKey()
    }

    // MARK: 退出快捷键 ⌥⌘Q
    //
    // 无边框窗口没有系统关闭按钮，之前加过一个"悬停才现的 ×"，
    // 用户反馈它破坏观感 —— 改成全局快捷键：
    // 用 Carbon 注册，**不需要窗口获得焦点**，所以不会把你正在用的窗口抢走。
    // 另有右键 / 长按 800ms 唤出菜单作为备用入口。

    func installQuitHotKey() {
        let sig = OSType(0x5742_5054)              // 'WBPT'
        let hotKeyID = EventHotKeyID(signature: sig, id: 1)
        let mods: UInt32 = UInt32(optionKey | cmdKey)   // ⌥⌘
        let st = RegisterEventHotKey(UInt32(kVK_ANSI_Q), mods, hotKeyID,
                                     GetEventDispatcherTarget(), 0, &quitHotKey)
        guard st == noErr else {
            NSLog("WorkBuddyPetPANIC: 快捷键注册失败(\(st))，请用右键菜单退出")
            return
        }
        NSLog("WorkBuddyPetOK: 退出快捷键 ⌥⌘Q 已注册")
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                                 eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetEventDispatcherTarget(), { _, _, _ -> OSStatus in
            DispatchQueue.main.async { NSApp.terminate(nil) }
            return noErr
        }, 1, &spec, nil, &quitHotKeyHandler)
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
        // 桌宠是独立页面（只含角色），不再是「用量报告」的一部分
        let base = baseURL.hasSuffix("/") ? baseURL : baseURL + "/"
        guard let url = URL(string: base + "pet.html") else { return }
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
        let mouse = NSEvent.mouseLocation
        let f = window.frame
        // 角色本体在画布里的位置：x 25%~75%，y（自底）10%~75%。
        // 只让这一块吃鼠标事件，其余（含画布留白）全部穿透。
        let hit = NSRect(x: f.minX + f.width * 0.23,
                         y: f.minY + f.height * 0.07,
                         width: f.width * 0.54,
                         height: f.height * 0.70)
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

        case "menu":
            dragging = false
            showMenu()

        case "quit":
            // 网页上的关闭按钮（悬停在角色右上角出现）
            dragging = false
            NSApp.terminate(nil)

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
        quit.keyEquivalentModifierMask = [.option, .command]   // 菜单里显示 ⌥⌘Q
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
