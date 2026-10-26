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
  var POLL_MS = 2000;         // 状态：2 秒一次（服务端 /state 实测 30~60ms）
  var METRICS_MS = 3000;      // 用量：3 秒一次（服务端增量解析后约 20ms）

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
  var taskButton = document.getElementById('petTasks');
  var tokenButton = document.getElementById('petTokens');
  var costLabel = document.getElementById('petCost');
  var tasks = [], selectedId = null, lastAgent = {}, today = null, tokenView = 0;
  var metricsAt = 0, metricsDay = '', metricsBusy = false, metricsLoaded = false;
  var sourceNames = {workbuddy: 'WB', codex: 'Codex', claude: 'Claude'};

  function showTask() {
    var selected = tasks.filter(function (t) { return t.id === selectedId; })[0];
    var agent = selected || lastAgent;
    anim.setState(effectiveState(agent.state));
    if (!taskButton) return;
    taskButton.hidden = !tasks.length;
    taskButton.textContent = tasks.length + ' 项任务';
    taskButton.title = selected ? (tasks.indexOf(selected) + 1) + '/' + tasks.length + ' · '
      + selected.label + '\n' + (selected.task || '暂无任务文字') + '\n' + (selected.cwd || '')
      + '\n点击切换观察任务' : '点击切换观察任务';
  }

  if (taskButton) taskButton.addEventListener('click', function () {
    if (!tasks.length) return;
    var index = tasks.findIndex(function (t) { return t.id === selectedId; });
    selectedId = tasks[(index + 1) % tasks.length].id;
    showTask();
  });

  function shortNumber(n) {
    if (n == null) return '—';
    if (n >= 100000000) return (n / 100000000).toFixed(1) + '亿';
    if (n >= 10000) return (n / 10000).toFixed(1) + '万';
    return n.toLocaleString();
  }
  function showTokens() {
    if (!tokenButton) return;
    if (!today) {
      // 首次拉取还没回来时不要写「—」：那会看起来像"没数据"，实际只是还在读。
      tokenButton.textContent = metricsLoaded ? '今日 Token —' : '今日 读取中…';
      tokenButton.title = metricsLoaded ? '用量暂不可读，正在自动重试' : '正在读取本机用量日志';
      return;
    }
    var names = Object.keys(sourceNames), key = names[tokenView - 1];
    tokenButton.textContent = tokenView
      ? sourceNames[key] + ' ' + shortNumber((today.bySource[key] || {}).totalTokens)
      : '今日 ' + shortNumber(today.totalTokens) + ' Token' + (today.complete ? '' : '*');
    tokenButton.title = today.dayLocal + ' · ' + today.note + '\n' + names.map(function (name) {
      var n = (today.bySource[name] || {}).totalTokens;
      return sourceNames[name] + ': ' + (n == null ? '未统计' : n.toLocaleString() + ' Token');
    }).join('\n') + '\n点击轮换来源（当前：' + (tokenView ? sourceNames[key] : '全部') + '）';
  }
  if (tokenButton) tokenButton.addEventListener('click', function () { tokenView = (tokenView + 1) % 4; showTokens(); });
  ['petSummary', 'petTokens'].forEach(function (id) {
    var el = document.getElementById(id);
    if (el) el.addEventListener('contextmenu', function (e) { e.stopPropagation(); });
  });
  function jsonAt(path) {
    return fetch(API + path, {cache: 'no-store'}).then(function (r) {
      if (!r.ok) throw new Error(r.status); return r.json();
    });
  }
  /* 卡片保持自动刷新（状态 2 秒、用量 3 秒），不显示更新时间：
     用户要求"只要会自己刷新就行"，界面上不多塞字。 */
  function pollMetrics() {
    if (!tokenButton && !costLabel) return;
    var day = new Date().toLocaleDateString();
    if (metricsBusy || (day === metricsDay && Date.now() - metricsAt < METRICS_MS)) return;
    metricsBusy = true; metricsAt = Date.now(); metricsDay = day;
    Promise.all([
      jsonAt('/usage').then(function (d) {
        today = d.today || (d.usage || {}).today || null;
        metricsLoaded = true;
        showTokens();
      }).catch(function () { today = null; metricsLoaded = true; showTokens(); }),
      jsonAt('/claude-cost').then(function (d) {
        if (!costLabel) return;
        var valid = d.verified && typeof d.amount === 'number' && isFinite(d.amount);
        var amount = valid ? (d.currency === 'USD' ? '$' : d.currency + ' ') + d.amount.toFixed(2) : '—';
        costLabel.textContent = '已花费 ' + amount;
        var dayAmount = (d.today && d.currency) ? Number(d.today[d.currency]) : null;
        costLabel.title = [
          d.note,
          dayAmount != null && isFinite(dayAmount)
            ? '今日 $' + dayAmount.toFixed(2) + ' · ' + (d.todayRequests || 0) + ' 次调用' : null,
          d.requests != null ? '累计 ' + d.requests + ' 次调用' : null,
          '来源：' + (d.source || '—'),
          '更新：' + (d.updatedAt || '—')
        ].filter(Boolean).join('\n');
      }).catch(function () { if (costLabel) { costLabel.textContent = '已花费 —'; costLabel.title = '费用暂不可读'; } })
    ]).then(function () { metricsBusy = false; });
  }

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
        lastAgent = (s || {}).agent || {};
        tasks = Array.isArray(s.tasks) ? s.tasks : [];
        if (!tasks.some(function (t) { return t.id === selectedId; })) {
          selectedId = tasks.some(function (t) { return t.id === lastAgent.id; }) ? lastAgent.id : null;
        }
        showTask();
      })
      .catch(function () { tasks = []; selectedId = null; lastAgent = {state: 'offline'}; showTask(); });
    pollMetrics();
  }

  function startPolling() {
    if (timer) return;
    /* 把「一个跑步动画周期多长」告诉宿主 —— 只报一次。
     为什么必须由网页报：manifest 只有这一侧知道，宿主拿不到。
     宿主需要它才能把"位移时长"对齐到整数个迈步周期；
     之前宿主里把这个值写死成 12帧/16fps，换素材后就对不上了。 */
  (function reportRunCycle() {
    var c = (window.PET_MANIFEST && window.PET_MANIFEST.clips || {}).run;
    if (c && c.fps && c.frameCount) {
      tell('runCycle', { seconds: c.frameCount / c.fps });
    }
  })();

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
  var lastClickAt = 0;      // 用于识别双击（双击 = 打开菜单）

  // 拖拽倾斜：素材画的是中性悬垂，"往哪边拖就倾向哪边"由运行时做
  function setTilt(deg) {
    var d = Math.max(-12, Math.min(12, deg));
    pet.style.setProperty('--pet-tilt', d.toFixed(1) + 'deg');
  }

  body.addEventListener('pointerdown', function (e) {
    // 右键 = 复位（跑回初始位置）。
    // **不依赖 contextmenu** —— 实测它在 WKWebView 里不触发
    //（日志证据：用户的点击有一堆 dragBegin/clicked，menu 一次都没有）。
    // pointerdown 的 button===2 是可靠的信号。
    if (e.button === 2) {
      if (!IS_DESKTOP) return;
      e.preventDefault();
      tell('reset');
      return;
    }
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

    // 双击 = 复位（跑回初始位置）。
    // 为什么值得单独加一条：`clicked` 是**实测最可靠**的信号（日志里一堆），
    // 而右键那条路（contextmenu）在 WKWebView 里根本不触发。
    // 用户要求"跑动不要出现在菜单里"，所以这里是**直接手势**，不弹菜单。
    var now2 = Date.now();
    var isDbl = IS_DESKTOP && !drag.moved && (now2 - lastClickAt) < 400;
    lastClickAt = now2;
    if (isDbl) {
      tell('reset');
      return;
    }

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

  /* 长按 800ms 唤出原生菜单（现在菜单里只剩「退出桌面宠物」）。
     为什么留着：无边框窗口没有关闭按钮，双击/右键已经改成"复位"了，
     所以需要一个**不用记快捷键**的退出口。
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
