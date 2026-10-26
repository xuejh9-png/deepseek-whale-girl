'use strict';
/*
 * Windows 11 桌面宿主 —— 与 desktop/main.swift（macOS）同一套交互，换成 Electron。
 *
 * 选 Electron 而不是原生 Win32/C# 的原因只有一条：
 *   前端（pet.html / web/* / assets/*）是现成的，Electron 能原样复用，
 *   而两套原生实现意味着两份动画逻辑，迟早会分叉。
 * 代价是安装包大得多（见 README 的「取舍」一节）。
 *
 * 平台差异（都标在对应位置）：
 *   · 坐标原点：Windows/Electron 左上，macOS/Cocoa 左下 —— 所有 hitbox 的 y 要翻过来
 *   · 层级：alwaysOnTop('floating')，不要用 screen-saver，会盖住全屏应用
 *   · 鼠标穿透：轮询 getCursorScreenPoint + setIgnoreMouseEvents(true, {forward:true})
 */

const { app, BrowserWindow, ipcMain, screen, Menu, globalShortcut, shell } = require('electron');
const fs = require('fs');
const path = require('path');

const { start: startServer } = require('./server');
const { hitTest } = require('./hitbox');

const WIN_W = 160;
const WIN_H = 200;
const MIN_VISIBLE_W = 48;      // 拖动时至少留这么多在屏内 —— 保证永远抓得回来
const MIN_VISIBLE_H = 56;
const PASSTHROUGH_MS = 50;

let win = null;
let origin = null;
let server = null;
let dragState = null;
let lastInside = null;
let passTimer = null;

const stateFile = () => path.join(app.getPath('userData'), 'window-state.json');

/* ---------------- 位置记忆 ---------------- */

function intersectsVisible(x, y) {
  const rect = { x, y, width: WIN_W, height: WIN_H };
  for (const display of screen.getAllDisplays()) {
    const a = display.workArea;
    const ix = Math.max(0, Math.min(rect.x + rect.width, a.x + a.width) - Math.max(rect.x, a.x));
    const iy = Math.max(0, Math.min(rect.y + rect.height, a.y + a.height) - Math.max(rect.y, a.y));
    if (ix >= MIN_VISIBLE_W && iy >= MIN_VISIBLE_H) return true;
  }
  return false;
}

function loadOrigin() {
  try {
    const raw = JSON.parse(fs.readFileSync(stateFile(), 'utf8'));
    if (Number.isFinite(raw.x) && Number.isFinite(raw.y) && intersectsVisible(raw.x, raw.y)) {
      return { x: Math.round(raw.x), y: Math.round(raw.y) };
    }
  } catch (err) {
    // 第一次启动没有这个文件，正常
  }
  return null;
}

function saveOrigin() {
  if (!win || win.isDestroyed()) return;
  const [x, y] = win.getPosition();
  try {
    fs.mkdirSync(path.dirname(stateFile()), { recursive: true });
    fs.writeFileSync(stateFile(), JSON.stringify({ x, y }), 'utf8');
  } catch (err) {
    console.warn('[pet] 位置没保存成功：', err.message);
  }
}

function defaultOrigin() {
  const area = screen.getPrimaryDisplay().workArea;
  return {
    x: Math.round(area.x + area.width - WIN_W - 24),
    y: Math.round(area.y + area.height - WIN_H - 24),
  };
}

function clampOrigin(x, y) {
  // Electron 的 workArea 是左上原点的 DIP 坐标，多屏有负数区域，必须按"最近的那块屏"夹
  const display = screen.getDisplayMatching({ x, y, width: WIN_W, height: WIN_H });
  const a = display.workArea;
  return {
    x: Math.min(Math.max(x, a.x - (WIN_W - MIN_VISIBLE_W)), a.x + a.width - MIN_VISIBLE_W),
    y: Math.min(Math.max(y, a.y - (WIN_H - MIN_VISIBLE_H)), a.y + a.height - MIN_VISIBLE_H),
  };
}

/* ---------------- 鼠标穿透 ---------------- */

function updatePassthrough() {
  if (!win || win.isDestroyed()) return;
  if (dragState) return;                      // 拖动中必须一直接收鼠标事件
  const pt = screen.getCursorScreenPoint();
  const inside = hitTest(pt, win.getBounds());
  if (inside === lastInside) return;
  lastInside = inside;
  // forward:true —— 穿透状态下仍把移动事件转给页面，否则拖不起来
  win.setIgnoreMouseEvents(!inside, { forward: true });
}

/* ---------------- 菜单 / 快捷键 ---------------- */

function showMenu() {
  if (!win || win.isDestroyed()) return;
  const menu = Menu.buildFromTemplate([
    {
      label: '退出桌面宠物',
      click: () => { app.quit(); },
    },
  ]);
  menu.popup({ window: win });
}

function registerShortcuts() {
  // 和 macOS 的 ⌥⌘Q 对齐：mac 上是 ⌘⌥Q，Windows/Linux 上是 Ctrl+Alt+Q。
  // 这条是穿透判定失效时唯一的退出口，必须有。
  const ok = globalShortcut.register('CommandOrControl+Alt+Q', () => app.quit());
  if (!ok) console.warn('[pet] 快捷键注册失败（可能被别的程序占用）');
}

/* ---------------- 网页指令 ---------------- */

function bindIpc() {
  ipcMain.on('pet-host', (event, msg) => {
    if (!msg || typeof msg !== 'object') return;
    if (!win || win.isDestroyed()) return;

    switch (msg.cmd) {
      case 'dragBegin':
        dragState = {
          mouse: screen.getCursorScreenPoint(),
          win: win.getPosition(),
        };
        break;

      case 'dragMove': {
        if (!dragState) break;
        const now = screen.getCursorScreenPoint();
        const next = clampOrigin(
          dragState.win[0] + (now.x - dragState.mouse.x),
          dragState.win[1] + (now.y - dragState.mouse.y),
        );
        win.setPosition(next.x, next.y, false);
        break;
      }

      case 'dragEnd':
        dragState = null;
        saveOrigin();
        lastInside = null;                   // 松手后立刻重算一次穿透
        updatePassthrough();
        break;

      case 'menu':
        dragState = null;
        showMenu();
        break;

      case 'clicked':
        // 角色反应由网页自己播动画，宿主不需要做事
        break;

      default:
        break;
    }
  });
}

/* ---------------- 窗口 ---------------- */

function createWindow() {
  const pos = loadOrigin() || defaultOrigin();
  win = new BrowserWindow({
    width: WIN_W,
    height: WIN_H,
    x: pos.x,
    y: pos.y,
    transparent: true,          // 窗口透明（Windows 上需要显卡支持，见 README）
    frame: false,
    resizable: false,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    hasShadow: false,
    skipTaskbar: true,          // 不占任务栏（等价于 macOS 的 LSUIElement）
    alwaysOnTop: true,
    show: false,
    backgroundColor: '#00000000',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,           // preload 里要用 ipcRenderer
      backgroundThrottling: false,   // 窗口不聚焦时动画不能停
    },
  });

  // 'floating' 而不是 'screen-saver'：后者会盖在全屏视频/游戏上面
  win.setAlwaysOnTop(true, 'floating');
  win.setVisibleOnAllWorkspaces(false);

  win.loadURL(`${origin}/pet.html`);

  win.once('ready-to-show', () => {
    win.showInactive();                            // 不抢当前窗口的焦点
    win.setIgnoreMouseEvents(true, { forward: true });
    lastInside = false;
    passTimer = setInterval(updatePassthrough, PASSTHROUGH_MS);
  });

  win.on('closed', () => {
    if (passTimer) { clearInterval(passTimer); passTimer = null; }
    win = null;
  });
}

// 网页里有 target=_blank 的链接时不要弹 Electron 窗口，交给系统浏览器
app.on('web-contents-created', (event, contents) => {
  contents.setWindowOpenHandler(({ url }) => {
    if (/^https?:/i.test(url)) shell.openExternal(url);
    return { action: 'deny' };
  });
});

/* ---------------- 生命周期 ---------------- */

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (win && !win.isDestroyed()) {
      win.showInactive();
      win.setAlwaysOnTop(true, 'floating');
    }
  });

  app.whenReady().then(async () => {
    if (process.platform === 'win32') {
      app.setAppUserModelId('com.xuejh.workbuddy.pet');
    }
    if (process.platform === 'darwin' && app.dock) {
      app.dock.hide();                             // macOS 上跑也不占 Dock
    }

    try {
      const started = await startServer();
      server = started.server;
      origin = started.origin;
      // 端口是系统分配的，打出来才能验收（也方便排查"页面连不上"）
      console.log(`[pet] 本地服务 ${origin}`);
      console.log(`[pet] 数据目录 ${require('./claude-code').CLAUDE_PROJECTS}`);
    } catch (err) {
      console.error('[pet] 本地服务起不来：', err);
      app.quit();
      return;
    }

    bindIpc();
    registerShortcuts();
    createWindow();
  });

  app.on('window-all-closed', () => { app.quit(); });

  app.on('will-quit', () => {
    globalShortcut.unregisterAll();
    if (passTimer) { clearInterval(passTimer); passTimer = null; }
    saveOrigin();
    if (server) { try { server.close(); } catch (err) { /* 已经关了 */ } }
  });
}
