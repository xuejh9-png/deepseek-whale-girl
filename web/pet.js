/* ============================================================
   WorkBuddy Pet —— 胶水层
   ------------------------------------------------------------
   这一层只做四件事，不含动画逻辑（动画在 pet-runtime.js）：
     1. 建运行时、开 rAF 循环（唯一的定时源）
     2. 拉 /state → 喂给状态机
     3. 指针事件 → 触发动作
     4. 桌面宿主（Swift）的消息桥
   ============================================================ */
(function () {
  'use strict';

  var pet = document.getElementById('pet');
  if (!pet) return;

  var body = document.getElementById('petBody');

  // 通过 daemon 打开时用同源；file:// 调试时连本地默认端口
  var API = /^https?:$/.test(location.protocol) ? '' : 'http://127.0.0.1:8791';
  var POLL_MS = 2500;

  // Swift 宿主注入的桥；纯浏览器里为 null
  var host = (window.webkit && window.webkit.messageHandlers &&
              window.webkit.messageHandlers.petHost) || null;
  var IS_DESKTOP = !!host;

  function tell(cmd, extra) {
    if (!host) return;
    var msg = { cmd: cmd };
    if (extra) {
      for (var k in extra) { if (extra.hasOwnProperty(k)) msg[k] = extra[k]; }
    }
    try { host.postMessage(msg); } catch (e) {}
  }

  /* ---------------- 动画运行时 ---------------- */

  var anim = new window.PetAnimation({
    spriteEl: document.getElementById('petSprite'),
    rootEl: pet,
    basePath: 'assets/pet/',
    manifestUrl: 'assets/pet/manifest.json'
  });

  anim.init().then(function () {
    pet.dataset.state = 'idle';
    anim.setState('idle');
    startLoop();
  });

  // 唯一的定时源：rAF + 时间累积。页面隐藏时停掉，回来再续。
  var last = 0, running = false, ticks = 0;

  function frame(now) {
    if (!running) return;
    var dt = last ? (now - last) : 16;
    last = now;
    if (dt > 250) dt = 250;        // 长时间挂起后不要一次补很多帧
    anim.tick(dt);
    pet.dataset.ticks = (++ticks);  // 便于外部验证 rAF 是否在跑
    requestAnimationFrame(frame);
  }

  function startLoop() {
    if (running) return;
    running = true;
    last = 0;
    requestAnimationFrame(frame);
  }

  function stopLoop() { running = false; }

  document.addEventListener('visibilitychange', function () {
    if (document.hidden) stopLoop();
    else startLoop();
  });

  // 窗口尺寸变化时单格像素尺寸会变，要重算 sheet 缩放
  window.addEventListener('resize', function () { anim.relayout(); });

  /* ---------------- 状态拉取 ---------------- */

  var timer = null;

  function poll() {
    fetch(API + '/state', { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (s) { anim.setState(((s || {}).agent || {}).state); })
      .catch(function () { anim.setState('offline'); });
  }

  function startPolling() {
    if (timer) return;
    poll();
    timer = setInterval(poll, POLL_MS);
  }

  document.addEventListener('visibilitychange', function () {
    if (document.hidden) { if (timer) { clearInterval(timer); timer = null; } }
    else startPolling();
  });

  startPolling();

  /* ---------------- 指针 → 动作 ---------------- */

  var drag = { on: false, moved: false, sx: 0, sy: 0, lastScreenX: 0 };

  // 拖拽倾斜：素材画的是中性悬垂，"往哪边拖就倾向哪边"由运行时做
  function setTilt(deg) {
    var d = Math.max(-12, Math.min(12, deg));
    pet.style.setProperty('--pet-tilt', d.toFixed(1) + 'deg');
  }

  body.addEventListener('pointerdown', function (e) {
    if (e.button !== 0) return;
    drag.on = true;
    drag.moved = false;
    drag.sx = e.clientX;
    drag.sy = e.clientY;
    drag.lastScreenX = e.screenX;
    try { body.setPointerCapture(e.pointerId); } catch (err) {}
    pet.classList.add('dragging');
    tell('dragBegin');
  });

  document.addEventListener('pointermove', function (e) {
    if (!drag.on) return;
    var dx = e.clientX - drag.sx, dy = e.clientY - drag.sy;
    if (!drag.moved && Math.abs(dx) + Math.abs(dy) > 4) {
      drag.moved = true;
      anim.trigger('grabbed', true);
    }
    if (!drag.moved) return;
    e.preventDefault();

    // 桌面模式窗口跟着指针走，clientX 不变 → 用屏幕坐标算方向
    var sx = e.screenX;
    var vx = sx - drag.lastScreenX;
    drag.lastScreenX = sx;
    setTilt(-vx * 1.6);

    if (IS_DESKTOP) tell('dragMove');
  }, { passive: false });

  document.addEventListener('pointerup', function () {
    if (!drag.on) return;
    drag.on = false;
    pet.classList.remove('dragging');
    setTilt(0);
    tell('dragEnd');

    if (drag.moved) {
      anim.trigger('released', true);      // 松开 → 下落 + 落地
    } else {
      anim.trigger('clicked', true);       // 没移动 = 点击 → 角色反应
      tell('clicked');
    }
  });

  // 指针离开窗口 / 被系统手势打断时也要收尾，否则会卡在 drag 剪辑里不落地
  document.addEventListener('pointercancel', function () {
    if (!drag.on) return;
    drag.on = false;
    pet.classList.remove('dragging');
    setTilt(0);
    tell('dragEnd');
    if (drag.moved) anim.trigger('released', true);   // 同样要播下落 + 落地
  });

  pet.addEventListener('contextmenu', function (e) {
    if (!IS_DESKTOP) return;
    e.preventDefault();
    tell('menu');
  });
})();
