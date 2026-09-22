/* ============================================================
   WorkBuddy Pet —— 行为层（桌面宠物）
   职责只有两件：
     1. 把 /state 的 agent.state 映射成角色的 data-state
     2. 把鼠标事件转给原生宿主（拖拽由 Swift 移动窗口）
   不显示任何 Token / Context / Model —— 那些不属于宠物。
   没有任何随机朝向 / 随机姿势。
   ============================================================ */
(function () {
  'use strict';

  var pet = document.getElementById('pet');
  if (!pet) return;

  var body = document.getElementById('petBody');

  // 通过 daemon 打开时用同源；直接用 file:// 打开调试时连本地默认端口
  var API = /^https?:$/.test(location.protocol) ? '' : 'http://127.0.0.1:8791';
  var POLL_MS = 2500;

  // Swift 宿主注入的桥；纯浏览器调试时为 null
  var host = (window.webkit && window.webkit.messageHandlers &&
              window.webkit.messageHandlers.petHost) || null;

  function tell(cmd, extra) {
    if (!host) return;
    var msg = { cmd: cmd };
    if (extra) {
      for (var k in extra) { if (extra.hasOwnProperty(k)) msg[k] = extra[k]; }
    }
    try { host.postMessage(msg); } catch (e) {}
  }

  /* ---------------- 状态映射 ---------------- */
  var KNOWN = {
    idle: 1, thinking: 1, working: 1, success: 1, waiting: 1,
    error: 1, sleeping: 1, offline: 1
  };

  function setState(s) {
    if (!s || !KNOWN[s]) s = 'idle';
    if (pet.dataset.state === s) return;
    pet.dataset.state = s;
  }

  /* ---------------- 轮询 ---------------- */
  var timer = null;

  function poll() {
    fetch(API + '/state', { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (s) {
        setState(((s || {}).agent || {}).state);
      })
      .catch(function () { setState('offline'); });
  }

  function start() {
    if (timer) return;
    poll();
    timer = setInterval(poll, POLL_MS);
  }

  function stop() {
    if (timer) { clearInterval(timer); timer = null; }
  }

  document.addEventListener('visibilitychange', function () {
    if (document.hidden) { stop(); setState('sleeping'); }
    else { start(); }
  });

  /* ---------------- 拖拽 ----------------
     桌面模式下位移由原生窗口负责，这里只负责"告诉宿主正在拖"。
     角色本体的抓起/落地反应在 S2 由动画剪辑接管。 */
  var drag = { on: false, moved: false, sx: 0, sy: 0 };

  body.addEventListener('pointerdown', function (e) {
    if (e.button !== 0) return;
    drag.on = true;
    drag.moved = false;
    drag.sx = e.clientX;
    drag.sy = e.clientY;
    try { body.setPointerCapture(e.pointerId); } catch (err) {}
    pet.classList.add('dragging');
    tell('dragBegin');
  });

  document.addEventListener('pointermove', function (e) {
    if (!drag.on) return;
    var dx = e.clientX - drag.sx, dy = e.clientY - drag.sy;
    if (!drag.moved && Math.abs(dx) + Math.abs(dy) > 4) drag.moved = true;
    if (!drag.moved) return;
    e.preventDefault();
    tell('dragMove');
  }, { passive: false });

  document.addEventListener('pointerup', function () {
    if (!drag.on) return;
    drag.on = false;
    pet.classList.remove('dragging');
    tell('dragEnd');
    // 没移动 = 点击。S1 不做任何 UI 反应，只把事件抛给宿主。
    if (!drag.moved) tell('clicked');
  });

  // 右键 → 原生轻量菜单（置顶 / 退出）
  document.addEventListener('contextmenu', function (e) {
    e.preventDefault();
    tell('menu');
  });

  /* ---------------- 启动 ---------------- */
  pet.dataset.state = 'idle';
  start();
})();
