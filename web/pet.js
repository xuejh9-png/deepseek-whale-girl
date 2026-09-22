/* ============================================================
   WorkBuddy Pet —— 行为层
   状态机：服务端 /state 的 agent.state → 角色姿态 + 特效 + HUD
   所有动画都在 CSS 里，这里只切 data-state，不逐帧跑 JS。
   ============================================================ */
(function () {
  'use strict';

  var pet = document.getElementById('pet');
  if (!pet) return;

  var body   = document.getElementById('petBody');
  var hud    = document.getElementById('petHud');
  var panel  = document.getElementById('petPanel');
  var views  = [].slice.call(pet.querySelectorAll('.pet-view'));

  var miniPct  = document.getElementById('miniPct');
  var fullCtx  = document.getElementById('fullCtx');
  var fullPct  = document.getElementById('fullPct');
  var fullModel= document.getElementById('fullModel');
  var pState   = document.getElementById('pState');
  var pModel   = document.getElementById('pModel');
  var pCtx     = document.getElementById('pCtx');
  var pMonth   = document.getElementById('pMonth');
  var pCalls   = document.getElementById('pCalls');
  var pTask    = document.getElementById('pTask');
  var pClose   = document.getElementById('pClose');

  // 通过 daemon 打开时用同源；双击 html（file://）时连本地默认端口
  var API = /^https?:$/.test(location.protocol) ? '' : 'http://127.0.0.1:8791';
  var STATIC = window.PET_STATIC || {};
  var POLL_MS = 2500;
  var POS_KEY = 'wb-pet-pos-v1';

  var STATE_TEXT = {
    idle: '空闲', thinking: '思考中', working: '执行中', success: '刚完成',
    waiting: '等你回复', error: '出错了', offline: '未连接', sleeping: '睡着啦'
  };

  // 桌面宠物（Swift 宿主）注入的桥；在普通浏览器里为 null
  var host = (window.webkit && window.webkit.messageHandlers &&
              window.webkit.messageHandlers.petHost) || null;
  var IS_DESKTOP = !!host;
  function tell(cmd, extra) {
    if (!host) return;
    var msg = { cmd: cmd };
    if (extra) { for (var k in extra) { if (extra.hasOwnProperty(k)) msg[k] = extra[k]; } }
    try { host.postMessage(msg); } catch (e) {}
  }

  /* ---------------- 数字格式化 ---------------- */
  function fmt(n) { return (n || 0).toLocaleString('en-US'); }
  function short(n) {
    if (n >= 1e6) return (n / 1e6).toFixed(n % 1e6 ? 1 : 0) + 'M';
    if (n >= 1e3) return Math.round(n / 1e3) + 'K';
    return String(n);
  }
  function human(n) {
    if (n >= 1e8) return (n / 1e8).toFixed(2) + ' 亿';
    if (n >= 1e4) return (n / 1e4).toFixed(1) + ' 万';
    return fmt(n);
  }
  function levelOf(pct) {
    if (pct == null) return 'na';
    if (pct < 50) return 'ok';
    if (pct < 75) return 'warn';
    if (pct < 90) return 'high';
    return 'critical';
  }

  /* ---------------- 状态应用 ---------------- */
  function setState(s) {
    if (!s) s = 'idle';
    if (pet.dataset.state === s) return;
    pet.dataset.state = s;
    pState.textContent = STATE_TEXT[s] || s;
  }

  function setTask(t) {
    pTask.textContent = t || '—';
  }

  function setUsage(u) {
    var pct = (u.contextUsagePercent == null) ? null : u.contextUsagePercent;
    var ctx = (u.contextTokens == null) ? null : u.contextTokens;
    var win = u.contextWindow;

    miniPct.textContent = (pct == null) ? '—' : pct + '%';
    hud.dataset.level = levelOf(pct);

    fullCtx.textContent = (ctx != null && win) ? fmt(ctx) + ' / ' + short(win) : '—';
    fullPct.textContent = (pct == null) ? 'Context —' : 'Context ' + pct + '%';
    fullModel.textContent = u.model || '—';

    pModel.textContent = u.model || '—';
    pCtx.textContent = (ctx != null)
      ? fmt(ctx) + (win ? ' / ' + short(win) : '') + (pct == null ? '' : ' (' + pct + '%)')
      : '—';
    pMonth.textContent = (u.totalTokens == null) ? '—' : human(u.totalTokens);
    pCalls.textContent = (u.calls == null) ? '—' : fmt(u.calls) + ' 次';
  }

  function applyOffline() {
    setState('offline');
    setUsage({
      contextTokens: null,
      contextWindow: null,
      contextUsagePercent: null,
      model: STATIC.model || null,
      totalTokens: (STATIC.totalTokens == null) ? null : STATIC.totalTokens,
      calls: (STATIC.calls == null) ? null : STATIC.calls
    });
    if (!pTask.textContent || pTask.textContent === '—') {
      pTask.textContent = '未连接到状态服务（双击「启动宠物.command」）';
    }
  }

  /* ---------------- 轮询 ---------------- */
  var timer = null;

  function poll() {
    fetch(API + '/state', { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (s) {
        var a = s.agent || {}, u = s.usage || {};
        setState(a.state);
        setTask(a.task);
        setUsage(u);
      })
      .catch(applyOffline);
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
    if (document.hidden) stop(); else start();
  });

  /* ---------------- 视图切换 ---------------- */
  function setView(i) {
    views.forEach(function (v, k) { v.classList.toggle('on', k === i); });
  }

  /* ---------------- 拖拽 + 位置记忆 ---------------- */
  var pos = null;
  var drag = { on: false, moved: false, sx: 0, sy: 0, ox: 0, oy: 0 };

  function clampPos(x, y) {
    var w = pet.offsetWidth, h = pet.offsetHeight;
    // 保证至少 60% 的角色留在可视区域内
    var minX = -w * 0.4, maxX = window.innerWidth - w * 0.6;
    var minY = -h * 0.4, maxY = window.innerHeight - h * 0.6;
    return {
      x: Math.max(minX, Math.min(maxX, x)),
      y: Math.max(minY, Math.min(maxY, y))
    };
  }

  function moveTo(x, y) {
    var p = clampPos(x, y);
    pet.style.left = p.x + 'px';
    pet.style.top = p.y + 'px';
    pet.style.right = 'auto';
    pet.style.bottom = 'auto';
    pos = p;
  }

  function savePos() {
    try { localStorage.setItem(POS_KEY, JSON.stringify(pos)); } catch (e) {}
  }
  function loadPos() {
    try {
      var s = localStorage.getItem(POS_KEY);
      var v = s ? JSON.parse(s) : null;
      return (v && typeof v.x === 'number') ? v : null;
    } catch (e) { return null; }
  }

  body.addEventListener('pointerdown', function (e) {
    if (e.button !== 0) return;
    drag.on = true;
    drag.moved = false;
    drag.sx = e.clientX;
    drag.sy = e.clientY;
    var r = pet.getBoundingClientRect();
    drag.ox = e.clientX - r.left;
    drag.oy = e.clientY - r.top;
    try { body.setPointerCapture(e.pointerId); } catch (err) {}
    pet.classList.add('dragging');
    if (IS_DESKTOP) tell('dragBegin');       // 桌面模式：位移交给原生窗口
  });

  document.addEventListener('pointermove', function (e) {
    if (!drag.on) return;
    var dx = e.clientX - drag.sx, dy = e.clientY - drag.sy;
    if (!drag.moved && Math.abs(dx) + Math.abs(dy) > 4) drag.moved = true;
    if (!drag.moved) return;
    if (IS_DESKTOP) { tell('dragMove'); return; }
    e.preventDefault();
    moveTo(e.clientX - drag.ox, e.clientY - drag.oy);
  }, { passive: false });

  document.addEventListener('pointerup', function () {
    if (!drag.on) return;
    drag.on = false;
    pet.classList.remove('dragging');
    if (IS_DESKTOP) {
      tell('dragEnd');
      if (!drag.moved) { togglePanel(); }    // 没移动 = 点击
      return;
    }
    if (drag.moved) savePos();
    else togglePanel();          // 没拖动 = 点击 → 开合面板
  });

  // 桌面模式：右键弹原生菜单（退出 / 置顶）
  pet.addEventListener('contextmenu', function (e) {
    if (!IS_DESKTOP) return;
    e.preventDefault();
    tell('menu');
  });

  window.addEventListener('resize', function () {
    if (pos) moveTo(pos.x, pos.y);
  });

  /* ---------------- 面板 ---------------- */
  function togglePanel() {
    panel.classList.toggle('open');
    tell('panel', { open: panel.classList.contains('open') });
  }
  pClose.addEventListener('click', function (e) {
    e.stopPropagation();
    panel.classList.remove('open');
    tell('panel', { open: false });
  });

  /* ---------------- 空闲时偶尔张望 ---------------- */
  function scheduleLook() {
    setTimeout(function () {
      var s = pet.dataset.state;
      if (!drag.on && !panel.classList.contains('open') &&
          (s === 'idle' || s === 'success' || s === 'waiting')) {
        var dir = Math.random() < 0.5 ? 1 : 3;   // left / right
        setView(dir);
        setTimeout(function () {
          if (!drag.on) setView(0);
        }, 900 + Math.random() * 900);
      }
      scheduleLook();
    }, 9000 + Math.random() * 9000);
  }

  /* ---------------- 启动 ---------------- */
  // 桌面宠物模式：窗口里只留角色，页面其余部分隐藏
  try {
    if (/[?&]desktop=1/.test(location.search)) {
      document.body.classList.add('desktop-mode');
    }
  } catch (e) {}

  pet.dataset.state = 'idle';
  pState.textContent = STATE_TEXT.idle;
  setUsage({
    contextTokens: STATIC.contextTokens, contextWindow: STATIC.contextWindow,
    contextUsagePercent: STATIC.contextUsagePercent, model: STATIC.model,
    totalTokens: STATIC.totalTokens, calls: STATIC.calls
  });

  pos = loadPos();
  if (pos) moveTo(pos.x, pos.y);

  start();
  scheduleLook();
})();
