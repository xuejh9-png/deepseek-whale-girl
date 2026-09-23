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

// MARK: - 日志
//
// 系统统一日志（NSLog）在受限环境里读不到（`log: Cannot run while sandboxed`），
// 而"她怎么突然不见了""右键为什么没反应"这类问题**必须**能从文件里查到原因，
// 不能靠猜。所以关键动作除 NSLog 之外再写一份到文件。
let APP_LOG = "/tmp/workbuddy-pet-app.log"

func petLog(_ msg: String) {
    NSLog("WorkBuddyPet: %@", msg)
    let ts = ISO8601DateFormatter().string(from: Date())
    guard let data = "\(ts) \(msg)\n".data(using: .utf8) else { return }
    if let fh = FileHandle(forWritingAtPath: APP_LOG) {
        fh.seekToEndOfFile()
        fh.write(data)
        try? fh.close()
    } else {
        FileManager.default.createFile(atPath: APP_LOG, contents: data)
    }
}


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
    var passTimer: Timer?
    var hotKeyRefs: [EventHotKeyRef?] = []
    var hotKeyHandler: EventHandlerRef?
    var hotKeyActions: [UInt32: () -> Void] = [:]

    // 跑动（「回到初始位置」时用 run 剪辑）
    var runTimer: Timer?
    var runFrom = NSPoint.zero
    var runTo = NSPoint.zero
    var runT0 = Date()
    var runDur: TimeInterval = 0

    // 网页推上来的当前状态，右键菜单里显示
    var lastState: String?
    var lastLabel: String?
    var lastTask: String?

    // 窗口 = 素材画布的实际显示尺寸（320×400 画布 @ 0.5 缩放）。
    // 角色本体占画布高度 64%，对应屏幕高度 128 CSS px。
    // 改大小：同时改 web/pet.css 的 --pet-char-height 和这两个值。
    let winW: CGFloat = 160
    let winH: CGFloat = 200

    // 拖动时至少要留在屏幕内这么多像素 —— 保证永远抓得回来。
    // 她之前可以被拖到完全出屏，用户只能靠 ⌥⌘Q 退出。
    let minVisibleW: CGFloat = 48
    let minVisibleH: CGFloat = 56

    let originKey = "petWindowOrigin"
    var selfTesting = false
    var daemonSpawnTried = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)      // 不占 Dock
        bridge.host = self
        petLog("启动 pid=\(ProcessInfo.processInfo.processIdentifier)")
        buildWindow()
        loadPage()
        watchMouse()
        installHotKeys()
    }

    func applicationWillTerminate(_ notification: Notification) {
        petLog("即将退出（保存位置）")
        saveOrigin()
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        petLog("收到退出请求")
        return .terminateNow
    }

    // MARK: 全局快捷键
    //
    // 无边框窗口没有系统关闭按钮，之前加过一个"悬停才现的 ×"，
    // 用户反馈它破坏观感 —— 改成全局快捷键：
    // 用 Carbon 注册，**不需要窗口获得焦点**，所以不会把你正在用的窗口抢走。
    //
    // ⌥⌘Q 退出 ／ ⌥⌘H 跑一下（回初始位置）
    //
    // ⚠️ 加 ⌥⌘H 的原因：鼠标那条路（右键菜单）依赖"窗口此刻吃不吃鼠标事件"，
    // 一旦穿透判定出问题，菜单就点不出来、用户完全不知道该怎么办。
    // 键盘这条路不经过鼠标命中测试，是**必然可用**的兜底。

    func installHotKeys() {
        registerHotKey(id: 1, key: UInt32(kVK_ANSI_Q), label: "⌥⌘Q 退出") { NSApp.terminate(nil) }
        registerHotKey(id: 2, key: UInt32(kVK_ANSI_H), label: "⌥⌘H 跑一下") { [weak self] in
            self?.goHomeOrLap()
        }
    }

    func registerHotKey(id: UInt32, key: UInt32, label: String,
                        action: @escaping () -> Void) {
        var ref: EventHotKeyRef?
        let hotKeyID = EventHotKeyID(signature: OSType(0x5742_5054), id: id)   // 'WBPT'
        let st = RegisterEventHotKey(key, UInt32(optionKey | cmdKey), hotKeyID,
                                     GetEventDispatcherTarget(), 0, &ref)
        guard st == noErr else {
            petLog("⚠️ 快捷键注册失败（\(label), err=\(st)）")
            return
        }
        hotKeyRefs.append(ref)
        hotKeyActions[id] = action
        petLog("已注册 \(label)")

        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                                 eventKind: UInt32(kEventHotKeyPressed))
        // handler 只装一次，靠 hotKeyID 分派
        if hotKeyHandler == nil {
            InstallEventHandler(GetEventDispatcherTarget(), { _, event, _ -> OSStatus in
                var hkID = EventHotKeyID()
                GetEventParameter(event, EventParamName(kEventParamDirectObject),
                                  EventParamType(typeEventHotKeyID), nil,
                                  MemoryLayout<EventHotKeyID>.size, nil, &hkID)
                DispatchQueue.main.async {
                    (NSApp.delegate as? AppDelegate)?.hotKeyActions[hkID.id]?()
                }
                return noErr
            }, 1, &spec, nil, &hotKeyHandler)
        }
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

        let restored = restoreOrigin()
        let o = restored ?? homeOrigin()
        window.setFrameOrigin(o)
        // 启动时把"位置从哪来"记下来 —— 「关掉再开能回到原位」这个承诺
        // 必须能在外面对着日志验，而不是靠相信代码。
        petLog("WorkBuddyPetOK: 窗口起点 x=\(Int(o.x)) y=\(Int(o.y)) 来源=\(restored != nil ? "记忆" : "默认位")")

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

    // MARK: 位置：默认位 / 记忆 / 边界约束
    //
    // 三件事都是为了同一个目标 —— **她永远丢不了**：
    //   1. 拖动时不让整只出屏（只留 minVisible 那点还在屏幕内）
    //   2. 关掉再打开回到上次的位置（而不是每次都跳回右下角）
    //   3. 换了显示器 / 改了分辨率后，旧坐标可能已经指向一片不存在的区域，
    //      这时必须回落到默认位，而不是把宠物"还"到一个看不见的地方

    /// 默认位：鼠标所在那块屏幕的右下角。
    /// 用鼠标所在屏而不是主屏 —— 多显示器时她才会出现在你正在看的地方。
    func homeOrigin() -> NSPoint {
        let mouse = NSEvent.mouseLocation
        let scr = NSScreen.screens.first { $0.frame.contains(mouse) } ?? NSScreen.main
        guard let f = scr?.visibleFrame else { return NSPoint(x: 40, y: 40) }
        return NSPoint(x: f.maxX - winW - 20, y: f.minY + 20)
    }

    /// 与给定矩形相交面积最大的屏幕；完全不相交时返回 nil
    func screenFor(_ rect: NSRect) -> NSScreen? {
        var best: NSScreen?
        var bestArea: CGFloat = 0
        for s in NSScreen.screens {
            let i = rect.intersection(s.visibleFrame)
            guard !i.isNull, !i.isEmpty else { continue }
            let a = i.width * i.height
            if a > bestArea { bestArea = a; best = s }
        }
        return best
    }

    /// 把窗口原点收进屏幕，保证至少 minVisible 的部分可见
    func clampOrigin(_ p: NSPoint) -> NSPoint {
        let r = NSRect(origin: p, size: NSSize(width: winW, height: winH))
        guard let vf = (screenFor(r) ?? NSScreen.main)?.visibleFrame else { return p }
        return NSPoint(
            x: min(max(p.x, vf.minX - (winW - minVisibleW)), vf.maxX - minVisibleW),
            y: min(max(p.y, vf.minY - (winH - minVisibleH)), vf.maxY - minVisibleH))
    }

    func saveOrigin() {
        guard window != nil, !selfTesting else { return }
        let o = window.frame.origin
        UserDefaults.standard.set([Double(o.x), Double(o.y)], forKey: originKey)
    }

    // MARK: 自检（--selftest-runhome）
    //
    // 跑动逻辑是"宿主移窗口 + 网页播动画"两半拼起来的，
    // 而移窗口这件事在无头环境里没法点菜单验证 —— 所以留一个入口：
    // 启动后自动挪开一段距离，再跑回初始位置，把轨迹打到 stderr 后自己退出。
    //
    //   desktop/WorkBuddyPet.app/Contents/MacOS/WorkBuddyPet --selftest-runhome
    //
    // 断言三件事：① 轨迹单调靠近（没有来回抖）② 最终落在初始位
    // ③ 全程没有掉帧式跳变（每步位移不超过步长上限）

    func runSelfTest() {
        selfTesting = true
        let home = clampOrigin(homeOrigin())

        // 先挪到一个明显偏离的位置（左侧 320px、上方 40px）
        window.setFrameOrigin(clampOrigin(NSPoint(x: home.x - 320, y: home.y + 40)))
        let from = window.frame.origin
        NSLog("WorkBuddyPetSELFTEST: from=(\(from.x),\(from.y)) home=(\(home.x),\(home.y))")

        var xs: [CGFloat] = []
        var bad = 0
        let obs = Timer.scheduledTimer(withTimeInterval: 1.0 / 30.0, repeats: true) { [weak self] t in
            guard let self = self else { t.invalidate(); return }
            xs.append(self.window.frame.origin.x)
            if self.runTimer == nil && xs.count > 3 {
                t.invalidate()
                var maxStep: CGFloat = 0
                for i in 1..<xs.count { maxStep = max(maxStep, abs(xs[i] - xs[i - 1])) }
                let landed = abs(xs.last! - home.x) < 1.0
                let yOK = abs(self.window.frame.origin.y - home.y) < 1.0
                let monotonic = zip(xs, xs.dropFirst()).allSatisfy { $0 <= $1 + 0.5 }
                bad = (monotonic ? 0 : 1) + (landed ? 0 : 1) + (yOK ? 0 : 1) + (maxStep > 30 ? 1 : 0)
                NSLog(String(format: "WorkBuddyPetSELFTEST: xs=%ld from=%.1f end=%.1f home=%.1f "
                             + "monotonic=%@ landed=%@ yOK=%@ maxStep=%.1f BAD=%ld",
                             xs.count, from.x, xs.last!, home.x,
                             monotonic ? "Y" : "N", landed ? "Y" : "N", yOK ? "Y" : "N",
                             maxStep, bad))
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { NSApp.terminate(nil) }
            }
        }
        _ = obs
        runHome()
    }

    /// 把 UserDefaults 取出的元素转成 Double。
    /// ⚠️ 四条路都要留着，缺一条就会静默丢位置：
    ///   `as? [Double]` 整体转型 → 元素是整数/字符串时直接失败
    ///   只试 `as? NSNumber`    → 从 [Any] 取出的数字不保证是 NSNumber
    ///   字符串                 → `defaults write -array 400 220` 存进去的就是字符串
    ///                            （实测确认过），手改过 plist 的用户会踩到
    func doubleOf(_ v: Any) -> Double? {
        if let d = v as? Double { return d }
        if let i = v as? Int { return Double(i) }
        if let n = v as? NSNumber { return n.doubleValue }
        if let s = v as? String { return Double(s) }
        return nil
    }

    /// 读回上次位置；若那次存的位置现在已经不可用则返回 nil（调用方回落默认位）
    func restoreOrigin() -> NSPoint? {
        guard let raw = UserDefaults.standard.array(forKey: originKey), raw.count == 2 else {
            return nil
        }
        let nums = raw.compactMap(doubleOf)
        guard nums.count == 2 else {
            petLog("WorkBuddyPetWARN: 记忆的位置格式不对（\(raw)），回落默认位")
            return nil
        }
        let p = NSPoint(x: CGFloat(nums[0]), y: CGFloat(nums[1]))
        let r = NSRect(origin: p, size: NSSize(width: winW, height: winH))
        // 每条失败路径都要留下日志：静默 return nil 会让「位置记不住」变成
        // 一个查不出来的现象（这次排查就吃过这个亏）
        guard let scr = screenFor(r) else {
            petLog("WorkBuddyPetWARN: 记住的位置(\(Int(p.x)),\(Int(p.y)))不在任何屏幕上，回落默认位")
            return nil
        }
        guard let vf = scr.visibleFrame as NSRect? else { return nil }
        let i = r.intersection(vf)
        guard !i.isNull, i.width >= minVisibleW, i.height >= minVisibleH else {
            petLog("WorkBuddyPetWARN: 记住的位置(\(Int(p.x)),\(Int(p.y)))只剩 \(Int(i.width))×\(Int(i.height)) 可见，回落默认位")
            return nil
        }
        return p
    }

    // MARK: 跑动（唯一真正使用 run 剪辑的场合）

    @objc func goHomeOrLap() {
        dragging = false
        runHome()
    }

    func runHome() {
        let home = clampOrigin(homeOrigin())
        var from = window.frame.origin

        // 先纵向归位、再横向跑：宠物是"站在地上"的，横着跑才讲得通。
        // 斜着跑会看起来像一边跑一边往上飘。
        if abs(from.y - home.y) > 1 {
            from.y = home.y
            window.setFrameOrigin(from)
        }

        let dx = home.x - from.x

        // ⚠️ 已经在原位时**不能什么都不做**。
        // 用户点"跑一下"却看到她一动不动，会直接认为功能坏了
        //（这次的反馈就是"我还是没看见她可以跑"）。
        // 所以在原位时改成跑个来回 —— 保证这个动作**永远看得见**。
        guard abs(dx) >= 40 else {
            let lap = lapAnchor(home: home)
            petLog("已在原位 → 跑个来回：\(Int(lap.x)) → \(Int(home.x))")
            startRun(dx: lap.x - from.x, target: lap, continuing: false) { [weak self] in
                guard let self = self else { return }
                self.startRun(dx: home.x - self.window.frame.origin.x,
                              target: home, continuing: true, then: nil)
            }
            return
        }
        startRun(dx: dx, target: home)
    }

    /// 原地折返的远端：往水平空间更宽的那一侧跑 120px
    func lapAnchor(home: NSPoint) -> NSPoint {
        let left = clampOrigin(NSPoint(x: home.x - 120, y: home.y))
        if abs(left.x - home.x) >= 60 { return left }
        return clampOrigin(NSPoint(x: home.x + 120, y: home.y))
    }

    /// continuing=true 表示"上一段还没停" —— 只翻方向、不重启剪辑，
    /// 否则折返处会看到她重新起跑一次（有明显顿挫）。
    func startRun(dx: CGFloat, target: NSPoint, continuing: Bool = false,
                  then: (() -> Void)? = nil) {
        let speed: CGFloat = 260                      // px/s
        runFrom = window.frame.origin
        runTo = target
        runDur = min(2.6, max(0.40, Double(abs(dx) / speed)))
        runT0 = Date()

        let dir: Int = dx < 0 ? -1 : 1
        if continuing {
            sendToPage("runFlip", ["dir": dir])
        } else {
            sendToPage("runStart", ["dir": dir, "durationMs": Int(runDur * 1000)])
        }
        petLog("跑动\(continuing ? "折返" : "开始") dir=\(dir) dx=\(Int(dx)) 用时\(String(format: "%.2f", runDur))s")

        runTimer?.invalidate()
        runTimer = Timer.scheduledTimer(withTimeInterval: 1.0 / 60.0, repeats: true) { [weak self] t in
            guard let self = self else { t.invalidate(); return }
            let k = min(1.0, Date().timeIntervalSince(self.runT0) / self.runDur)
            let eased = 1 - pow(1 - k, 2)             // 起步快、收尾稳
            let x = self.runFrom.x + (self.runTo.x - self.runFrom.x) * CGFloat(eased)
            self.window.setFrameOrigin(NSPoint(x: x, y: self.runTo.y))
            if k >= 1.0 {
                t.invalidate()
                self.runTimer = nil
                if let more = then {
                    more()                            // 还有下一段
                } else {
                    self.sendToPage("runEnd", [:])
                    self.saveOrigin()
                    petLog("跑动结束 x=\(Int(self.window.frame.origin.x))")
                }
            }
        }
    }

    func sendToPage(_ name: String, _ payload: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: payload),
              let json = String(data: data, encoding: .utf8) else { return }
        webView.evaluateJavaScript("window.petHostEvent && window.petHostEvent(\"\(name)\", \(json));",
                                   completionHandler: nil)
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

    /// 端口通不通 —— 直接用裸 socket connect。
    ///
    /// ⚠️ **不要用 URLSession 做这个探测。** 实测：`open` 启动的实例上，
    /// URLSession 的请求会**悬住不返回**（连 completion 都不进，日志里一行都没有），
    /// 结果是服务永远拉不起来、宠物永远退回"独立模式"，
    /// 而这个工具的**主要价值就是状态联动**。前台从终端起同一个二进制却正常 —— 典型的
    /// "受会话/代理层影响"。裸 socket 不走系统代理、不经过任何会话层，结果确定。
    func portOpen(_ port: Int) -> Bool {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return false }
        defer { close(fd) }
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = UInt16(port).bigEndian
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        // loopback 上 connect 要么立刻成功、要么立刻 ECONNREFUSED，不会久等
        let rc = withUnsafePointer(to: &addr) { p -> Int32 in
            p.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                connect(fd, sa, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        return rc == 0
    }

    func probeDaemon(attempt: Int = 0) {
        let port = URL(string: baseURL)?.port ?? 8791

        guard portOpen(port) else {
            if !daemonSpawnTried {
                daemonSpawnTried = true
                petLog("端口 \(port) 未监听 → 自己拉起状态服务")
                spawnDaemon()
            }
            // 拉起要几秒；多试几次，别只探一次就放弃
            if attempt < 8 {
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.0) { [weak self] in
                    self?.probeDaemon(attempt: attempt + 1)
                }
            } else {
                petLog("⚠️ 状态服务始终没起来，保持独立模式（看她仍然正常，只是不跟状态联动）")
            }
            return
        }

        // 端口通了 → 换成 http 版本。这一步用 WKWebView 自己的网络栈加载，
        // 和上面那个探测无关（探测只用 socket，不受会话层影响）。
        let base = baseURL.hasSuffix("/") ? baseURL : baseURL + "/"
        guard let url = URL(string: base + "pet.html") else { return }
        var req = URLRequest(url: url)
        req.timeoutInterval = 5
        webView.load(req)
        petLog("已连接状态服务 \(base)（第 \(attempt + 1) 次探测）")
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
            petLog("WorkBuddyPetWARN: 找不到本地 pet.html（\(pet.path)）")
            return
        }
        webView.loadFileURL(pet, allowingReadAccessTo: root)
    }

    /// 找不到状态服务就自己起一个（等价于双击 启动桌面宠物.command 里那一步）
    func spawnDaemon() {
        let root = projectRoot()
        let script = root.appendingPathComponent("pet_daemon.py")
        guard FileManager.default.fileExists(atPath: script.path) else {
            petLog("WorkBuddyPetWARN: 找不到 pet_daemon.py，只能以独立模式运行")
            return
        }
        // 与 启动桌面宠物.command 保持同一份候选顺序
        let candidates = [
            "\(NSHomeDirectory())/.local/bin/python3",
            "\(NSHomeDirectory())/.workbuddy/binaries/python/versions/3.13.12/bin/python3",
            "/opt/homebrew/bin/python3",
            "/usr/bin/python3",
        ]
        guard let py = candidates.first(where: { FileManager.default.isExecutableFile(atPath: $0) })
                ?? Self.whichPython() else {
            petLog("WorkBuddyPetWARN: 找不到 python3，只能以独立模式运行")
            return
        }

        let port = URL(string: baseURL)?.port ?? 8791
        let p = Process()
        p.executableURL = URL(fileURLWithPath: py)
        p.arguments = [script.path, "--port", "\(port)"]
        p.currentDirectoryURL = root
        // 日志别丢：服务起不来时这是唯一的线索
        let logPath = "/tmp/workbuddy-pet.log"
        if !FileManager.default.fileExists(atPath: logPath) {
            FileManager.default.createFile(atPath: logPath, contents: nil)
        }
        if let fh = FileHandle(forWritingAtPath: logPath) {
            p.standardOutput = fh
            p.standardError = fh
        }
        do {
            try p.run()
            petLog("WorkBuddyPetOK: 已自动拉起状态服务 pid=\(p.processIdentifier)（\(py)）")
        } catch {
            petLog("WorkBuddyPetWARN: 拉起状态服务失败 \(error.localizedDescription)")
        }
    }

    /// 兜底：从 PATH 里找 python3
    static func whichPython() -> String? {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        p.arguments = ["which", "python3"]
        let pipe = Pipe()
        p.standardOutput = pipe
        do { try p.run() } catch { return nil }
        p.waitUntilExit()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        let s = String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines)
        return (s?.isEmpty == false) ? s : nil
    }

    // MARK: 鼠标穿透 —— 只有角色所在的区域吃鼠标事件
    //
    // ⚠️ 这里曾经只依赖 `addGlobalMonitorForEvents(.mouseMoved)` 来更新判定，
    // 而**全局监视器收不到发给自己 App 的事件** —— 一旦光标停在我们窗口上，
    // 判定就可能停在某个过期值上：角色区域反而穿透，于是
    // "右键点了没反应""拖不动她"这类现象就出现了，而且完全没有日志可查。
    //
    // 改成**轮询鼠标位置**（20Hz，只读一个坐标，代价可忽略）：
    // 与事件投递路径完全解耦，判定永远不会过期。

    func watchMouse() {
        // 全局监视器留着（离开本窗口时立刻生效，比轮询更跟手）
        moveMonitor = NSEvent.addGlobalMonitorForEvents(matching: [.mouseMoved]) { [weak self] _ in
            self?.updatePassthrough()
        }
        updatePassthrough()
        passTimer = Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { [weak self] _ in
            self?.updatePassthrough()
        }
    }

    func updatePassthrough() {
        guard !dragging else { return }
        let mouse = NSEvent.mouseLocation
        let f = window.frame
        // 角色本体在画布里的位置：x 约 18%~82%，y（自底）10%~75%。
        // 留一点余量，让"点她"更容易命中；其余（含画布留白）仍然穿透。
        let hit = NSRect(x: f.minX + f.width * 0.16,
                         y: f.minY + f.height * 0.05,
                         width: f.width * 0.68,
                         height: f.height * 0.76)
        let inside = hit.contains(mouse)
        if window.ignoresMouseEvents == inside {     // 需要翻转时才动 + 记日志
            window.ignoresMouseEvents = !inside
            petLog("鼠标穿透切换 → \(inside ? "接住鼠标（在角色上）" : "穿透")")
        }
    }

    // MARK: 处理网页指令

    func handle(cmd: String, body: [String: Any]) {
        if cmd != "dragMove" { petLog("收到网页指令：\(cmd)") }
        switch cmd {
        case "dragBegin":
            dragging = true
            dragStartMouse = NSEvent.mouseLocation
            dragStartWin = window.frame.origin

        case "dragMove":
            let m = NSEvent.mouseLocation
            // 拖动过程中就夹住边界：否则用户可以把整只拖出屏幕，再也抓不回来
            window.setFrameOrigin(clampOrigin(NSPoint(x: dragStartWin.x + (m.x - dragStartMouse.x),
                                                      y: dragStartWin.y + (m.y - dragStartMouse.y))))

        case "dragEnd":
            dragging = false
            window.setFrameOrigin(clampOrigin(window.frame.origin))
            saveOrigin()                      // 记住这次放的位置
            updatePassthrough()

        case "state":
            // 网页每次轮询都会推上来，供右键菜单显示
            lastState = body["state"] as? String
            lastLabel = body["label"] as? String
            lastTask = body["task"] as? String

        case "menu":
            dragging = false
            showMenu()

        case "quit":
            dragging = false
            NSApp.terminate(nil)

        default:
            break
        }
    }

    /// 状态 → 中文（网页没给 label 时的兜底）
    func stateText() -> String {
        let map = ["idle": "空闲", "thinking": "思考中", "working": "执行中",
                   "success": "完成", "error": "出错", "waiting": "等待",
                   "sleeping": "睡眠", "offline": "离线（独立模式）"]
        if let l = lastLabel, !l.isEmpty { return l }
        if let s = lastState { return map[s] ?? s }
        return "—"
    }

    // MARK: 右键菜单
    //
    // 无边框窗口没有关闭按钮，退出走这里；
    // 第一行是只读的"她此刻在干什么"—— 这个工具的用途就是让人一眼知道 AI 的状态，
    // 菜单是唯一不用遮挡桌面的查看位置。

    func showMenu() {
        let menu = NSMenu()

        var line = "当前：\(stateText())"
        if let t = lastTask, !t.isEmpty {
            let one = t.replacingOccurrences(of: "\n", with: " ")
            line += " · " + (one.count > 18 ? String(one.prefix(18)) + "…" : one)
        }
        let info = NSMenuItem(title: line, action: nil, keyEquivalent: "")
        info.isEnabled = false
        menu.addItem(info)
        menu.addItem(NSMenuItem.separator())

        let home = NSMenuItem(title: "跑一下（她会跑给你看）",
                              action: #selector(goHomeOrLap), keyEquivalent: "h")
        home.keyEquivalentModifierMask = [.option, .command]   // 菜单里显示 ⌥⌘H
        home.target = self
        menu.addItem(home)

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
if argv.contains("--selftest-runhome") {
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { delegate.runSelfTest() }
}
// 写入一次当前位置并退出 —— 用于验证「程序自己写、程序自己读」这条往返链路
// （不要用 `defaults write` 造测试数据：它会把数字存成字符串，验的不是真实链路）
if argv.contains("--selftest-save") {
    DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) {
        delegate.selfTesting = false
        delegate.saveOrigin()
        petLog("WorkBuddyPetOK: 自检已保存当前起点并退出")
        NSApp.terminate(nil)
    }
}

app.delegate = delegate
app.run()
