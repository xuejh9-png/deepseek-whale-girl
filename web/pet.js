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
  pet.classList.toggle('has-host', IS_DESKTOP);

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

  /* ---------------- 睡着时被惊动：起身 + 保持清醒 ----------------
     不做这个的话，点她一下 → 醒一下 → 下一个轮询又把她按回去睡，
     看起来就是"点了没用"（用户反馈："她被我打醒了，但是不动就又睡着了"）。

     起身**不需要新素材**：sleep 的开场段是"躺下去"（1→8），
     把当前帧往回倒放就是"站起来"。姿势天然对得上，因为是同一组图。 */

  // 被碰过之后至少清醒这么久才可能再睡。
  // 可用 ?wakeMs=900 或 #wakeMs=900 覆盖 —— 只有一个目的：让回归测试
  // 能在几秒内跑完"睡着 → 点醒 → 待机 → 又睡着"整轮循环。
  var WAKE_MS = (function () {
    var m = /[?&#]wakeMs=(\d+)/.exec(location.search + location.hash);
    return m ? parseInt(m[1], 10) : 60000;
  })();
  var wakeUntil = 0;

  function awakeNow() { return Date.now() < wakeUntil; }

  // 状态覆盖：睡着但刚被碰过 → 当清醒处理
  function effectiveState(s) {
    return (s === 'sleeping' && awakeNow()) ? 'idle' : s;
  }

  function poll() {
    fetch(API + '/state', { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (s) {
        var a = (s || {}).agent || {};
        anim.setState(effectiveState(a.state));
        // 顺带把状态推给宿主，右键菜单里就能显示"她此刻在干什么"
        tell('state', { state: a.state, label: a.label, task: a.task,
                        source: (s || {}).source });
      })
      .catch(function () { anim.setState(effectiveState('offline')); tell('state', { state: 'offline' }); });
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
  var pressWasAsleep = false;

  // 拖拽倾斜：素材画的是中性悬垂，"往哪边拖就倾向哪边"由运行时做
  function setTilt(deg) {
    var d = Math.max(-12, Math.min(12, deg));
    pet.style.setProperty('--pet-tilt', d.toFixed(1) + 'deg');
  }

  body.addEventListener('pointerdown', function (e) {
    if (e.button !== 0) return;
    // 任何一次按下都算"你还在"，让她保持清醒一分钟
    pressWasAsleep = (anim.currentClip() === 'sleep');
    wakeUntil = Date.now() + WAKE_MS;
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
    } else if (pressWasAsleep) {
      // 她在睡 → 起身。顺序很重要：
      //   ① 先 trigger（占住 action，播倒放的 sleep）
      //   ② 再把状态改成 idle —— 此时 action 非空，setState 不会立刻打断动画，
      //      但已经把"目标状态"改了，起身播完 applyState 就会接 idle 而不是睡回去
      // 反过来做的话，setState 会立刻切到站立 idle，先跳一下再起身。
      anim.trigger('wakeUp', true);
      anim.setState('idle');
      tell('clicked');
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

  /* 长按 800ms 唤出原生菜单（置顶 / 退出）—— 不需要可见按钮，
     也不像右键那样要求用户先知道"可以右键"。
     注意：**不要在 pointermove 时清计时器** —— 按住时手指/鼠标抖 1px
     就会触发 pointermove，长按会永远按不出来。
     改成让计时器自己检查 drag.moved：真拖动了就不弹菜单。 */
  var pressTimer = null;
  body.addEventListener('pointerdown', function () {
    if (!IS_DESKTOP) return;
    clearTimeout(pressTimer);
    pressTimer = setTimeout(function () {
      if (drag.on && !drag.moved) {      // 按住且没移动 = 长按
        drag.on = false;                 // 吃掉这次手势，pointerup 不再触发点击/松手
        pet.classList.remove('dragging');
        tell('dragEnd');
        tell('menu');
      }
    }, 800);
  });
  ['pointerup', 'pointercancel'].forEach(function (t) {
    document.addEventListener(t, function () { clearTimeout(pressTimer); });
  });

  /* ---------------- 宿主 → 网页 的事件 ----------------
     Swift 用 evaluateJavaScript 调 window.petHostEvent(name, payload)。
     目前只服务「跑回初始位置」：**窗口移动是宿主的活，动画是网页的活**，
     两边各管一半，不互相猜。 */

  window.petHostEvent = function (name, payload) {
    payload = payload || {};
    if (name === 'runStart') {
      // dir < 0 = 向左跑。运行时按 manifest 的 flipForLeft 决定是否翻转。
      anim.trigger(payload.dir < 0 ? 'runLeft' : 'runRight', true);
    } else if (name === 'runFlip') {
      // 折返：只改朝向，**不重启剪辑** —— 重启会在折返处看到明显顿挫
      if (anim.setFacing) anim.setFacing(payload.dir < 0 ? -1 : 1);
    } else if (name === 'runEnd') {
      // run 是循环剪辑，自己永远不会"播完"，必须由宿主显式收尾，
      // 否则她会一直原地跑（和之前 drag 不落地的坑同一个成因）。
      anim.clearActions();
    }
  };
})();
