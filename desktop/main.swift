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

    // MARK: 载入
    //
    // 两种模式下都要能跑：
    //   有状态服务 → 从 http 载入，宠物会跟着 Agent 状态变
    //   没有服务   → 从本地文件载入，宠物只做自己的 idle 动画（独立可用）
    // 所以先立刻用本地文件把界面显示出来（不用等），再探测服务，
    // 通了就升级到 http 版本。

    func loadPage() {
        loadLocalFile()                       // 立刻显示，不等网络
        probeDaemon()                         // 后台探测，通了再升级
    }

    /// 项目根目录。
    /// 正常是 <项目>/desktop/WorkBuddyPet.app，所以从 bundle 上溯两层。
    /// 直接把二进制拿出来单跑时（没有 .app 外壳），改从可执行文件路径上溯。
    func projectRoot() -> URL {
        let b = Bundle.main.bundleURL
        if b.pathExtension == "app" {
            return b.deletingLastPathComponent().deletingLastPathComponent()
        }
        var u = URL(fileURLWithPath: CommandLine.arguments[0]).standardizedFileURL
        for _ in 0..<4 { u = u.deletingLastPathComponent() }
        return u
    }

    func loadLocalFile() {
        let root = projectRoot()
        let pet = root.appendingPathComponent("pet.html")
        guard FileManager.default.fileExists(atPath: pet.path) else {
            NSLog("WorkBuddyPetWARN: 找不到本地 pet.html（\(pet.path)）")
            return
        }
        webView.loadFileURL(pet, allowingReadAccessTo: root)
    }

    func probeDaemon() {
        let base = baseURL.hasSuffix("/") ? baseURL : baseURL + "/"
        guard let url = URL(string: base + "pet.html") else { return }
        var req = URLRequest(url: url)
        req.timeoutInterval = 1.5
        URLSession.shared.dataTask(with: req) { [weak self] _, resp, _ in
            guard let self = self else { return }
            let ok = (resp as? HTTPURLResponse)?.statusCode == 200
            guard ok else { return }
            DispatchQueue.main.async {
                // 服务在跑 → 换成 http 版本，这样状态联动才生效
                self.webView.load(req)
                NSLog("WorkBuddyPetOK: 已连接状态服务 \(base)")
            }
        }.resume()
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
