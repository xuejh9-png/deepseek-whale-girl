/* ============================================================
   WorkBuddy Pet —— 动画运行时
   ------------------------------------------------------------
   职责三件：
     1. Sprite 播放器 —— 按 manifest.json 的 fps 播帧，
        循环剪辑只循环 loopStart..loopEnd（不重播开场段）
     2. 状态机 —— 持续状态（idle/thinking/working/…）→ 对应剪辑；
        没有该剪辑时退化为 idle，并把兜底交回 CSS
     3. 动作队列 —— 瞬时动作（clicked/grabbed/released/…）
        优先于状态，播完自动回到当前状态
   性能：用 rAF + 时间累积，**只在真正换帧时碰 DOM**；
        不使用 setInterval，不逐帧写样式。
   ============================================================ */
(function (global) {
  'use strict';

  // 取不到 manifest 时的最小兜底（file:// 直接打开时 fetch 会被拦）
  var FALLBACK_MANIFEST = {
    version: 4,
    spriteSheet: { frameWidth: 320, frameHeight: 400, columns: 6 },
    clips: {
      idle: { file: 'idle.png', frameCount: 20, rows: 4, fps: 10,
              loop: true, loopStart: 1, loopEnd: 20 }
    }
  };

  // 状态 → 剪辑名。名字是素材侧的约定，不要在这里改语义。
  var STATE_CLIP = {
    idle:     'idle',
    thinking: 'think',
    working:  'work',
    waiting:  'idle',
    success:  'success',   // 第 3 批素材
    error:    'error',     // 第 3 批素材
    sleeping: 'sleep',     // 第 3 批素材
    offline:  'idle'
  };

  // 动作 → 剪辑名。缺剪辑时该动作会被跳过（交回宿主/CSS）
  var ACTION_CLIP = {
    clicked:  'click',   // 第 2 批素材
    grabbed:  'drag',    // 第 2 批素材
    dragging: 'drag',    // 第 2 批素材
    released: 'jump',    // 第 2 批素材（从 airborneFrame 起播 = 下落+落地）
    runLeft:  'run',     // 第 3 批素材
    runRight: 'run'
  };

  function PetAnimation(opts) {
    this.el = opts.spriteEl;
    this.root = opts.rootEl || document.body;
    this.basePath = opts.basePath || '';
    this.manifestUrl = opts.manifestUrl || '';

    this.manifest = null;
    this.loaded = {};        // clipName -> true 表示图片已就绪
    this.imageCache = {};

    this.state = 'idle';
    this.clipName = null;
    this.meta = null;
    this.frame = 0;          // 0 基
    this.acc = 0;
    this.action = null;      // 正在播的瞬时动作
    this.queue = [];
    this.done = false;       // 当前一次性剪辑是否已播完
    this.onActionDone = opts.onActionDone || null;
  }

  /* ---------------- 装载 ---------------- */

  PetAnimation.prototype.init = function () {
    var self = this;

    // 优先用 <script src="assets/pet/manifest.js"> 预置在 window 上的清单 ——
    // file:// 下 fetch 会被 CORS 拦，但 <script> 不受限制，
    // 这样"双击 pet.html 直接看"也能拿到全部剪辑（否则只剩 idle）。
    if (global.PET_MANIFEST && global.PET_MANIFEST.clips) {
      this.manifest = global.PET_MANIFEST;
      this.applyState();
      return Promise.resolve();
    }

    return fetch(this.manifestUrl, { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (m) { self.manifest = m; })
      .catch(function () {
        // 两条路都不通：用兜底清单继续（只有 idle 一个动作）
        self.manifest = FALLBACK_MANIFEST;
        self.fallbackManifest = true;
      })
      .then(function () {
        self.applyState();
      });
  };

  PetAnimation.prototype.hasClip = function (name) {
    return !!(name && this.manifest && this.manifest.clips && this.manifest.clips[name]);
  };

  /* ---------------- 状态 ---------------- */

  PetAnimation.prototype.setState = function (s) {
    if (!s || s === this.state) return;
    this.state = s;
    this.root.dataset.state = s;
    // 正在播瞬时动作时不要打断它，等它自己播完再回到新状态
    if (!this.action) this.applyState();
  };

  PetAnimation.prototype.applyState = function () {
    var want = STATE_CLIP[this.state] || 'idle';
    var use = this.hasClip(want) ? want : 'idle';
    // 没有该状态的专属剪辑 → 打开兜底开关，让 CSS 的姿态动画接管
    var fb = (!this.hasClip(want)) ? '1' : '0';
    if (this.root.dataset.fallback !== fb) this.root.dataset.fallback = fb;
    if (use !== this.clipName) this.playClip(use, null);
  };

  /* ---------------- 播放 ---------------- */

  // opts: null=按剪辑自身 loop 配置；{once:true}=只播一遍；{from:帧号}
  PetAnimation.prototype.playClip = function (name, opts) {
    var meta = this.manifest.clips[name];
    if (!meta) return false;
    opts = opts || {};

    this.clipName = name;
    this.meta = meta;
    this.frame = opts.from ? Math.max(0, opts.from - 1) : 0;
    this.acc = 0;
    this.done = false;
    this.loopThis = (meta.loop && !opts.once);

    var self = this;
    var url = this.basePath + meta.file;

    // ⚠️ 无论该剪辑是否已缓存，都必须把背景图切过去。
    // 曾经只在"首次加载"时设 backgroundImage，切回已加载过的剪辑时就沿用上一张
    // sheet —— 帧号按新剪辑走、画面却是旧剪辑的，症状是"显示落地帧却在循环"。
    if (this.el.style.backgroundImage.indexOf(url) === -1) {
      this.el.style.backgroundImage = 'url("' + url + '")';
    }

    if (this.loaded[name]) {
      this.relayout();
      this.renderFrame(true);
    } else {
      var img = this.imageCache[name] || new Image();
      this.imageCache[name] = img;
      img.onload = function () {
        self.loaded[name] = true;
        if (self.clipName === name) { self.relayout(); self.renderFrame(true); }
      };
      img.src = url;
    }
    return true;
  };

  // 按元素实际尺寸算 sheet 缩放，换剪辑 / 窗口变化时都要重算
  PetAnimation.prototype.relayout = function () {
    var ss = this.manifest.spriteSheet;
    var cols = ss.columns || 6;
    var rows = (this.meta && this.meta.rows) || 1;
    var w = this.el.clientWidth || 1;
    var h = this.el.clientHeight || 1;
    this.el.style.backgroundSize = (w * cols) + 'px ' + (h * rows) + 'px';
    this.cellW = w;
    this.cellH = h;
  };

  PetAnimation.prototype.renderFrame = function (force) {
    if (this.frame === this.lastFrame && !force) return;
    this.lastFrame = this.frame;
    var cols = this.manifest.spriteSheet.columns || 6;
    var col = this.frame % cols;
    var row = Math.floor(this.frame / cols);
    this.el.style.backgroundPosition = (-col * this.cellW) + 'px ' + (-row * this.cellH) + 'px';
    this.root.dataset.frame = this.frame;      // 便于外部验证取帧是否正确
  };

  /* ---------------- 时间推进 ---------------- */

  PetAnimation.prototype.tick = function (dtMs) {
    if (!this.meta) return;
    var fps = this.meta.fps || 10;
    var frameMs = 1000 / fps;
    this.acc += dtMs;
    var guard = 0;
    var changed = false;
    while (this.acc >= frameMs && guard++ < 8) {
      this.acc -= frameMs;
      var before = this.frame;
      this.advance();
      if (this.frame !== before) changed = true;
      if (this.done) break;              // 一次性剪辑播完就停
    }
    // 一个 tick 最多渲染一次：帧号没变就不碰 DOM
    if (changed) this.renderFrame();
  };

  PetAnimation.prototype.advance = function () {
    var m = this.meta;
    var n = m.frameCount;
    if (this.loopThis) {
      var s = (m.loopStart || 1) - 1;
      var e = (m.loopEnd || n) - 1;
      this.frame = (this.frame >= e) ? s : this.frame + 1;
      return;
    }
    if (this.frame >= n - 1) {
      this.done = true;
      this.finishOneShot();
    } else {
      this.frame++;
    }
  };

  PetAnimation.prototype.finishOneShot = function () {
    var finished = this.action;
    this.action = null;
    if (finished && this.onActionDone) this.onActionDone(finished);
    this.applyState();               // 回到当前持续状态
    this.nextInQueue();
  };

  /* ---------------- 动作队列 ---------------- */

  // 逐个取出直到有一个能真正启动（缺素材的条目会被跳过，不能卡住队列）
  PetAnimation.prototype.nextInQueue = function () {
    if (this.action) return;
    while (this.queue.length) {
      if (this.startAction(this.queue.shift())) return;
    }
  };

  // 返回是否真的启动了
  PetAnimation.prototype.startAction = function (item) {
    var clip = ACTION_CLIP[item.name];
    if (!this.hasClip(clip)) return false;   // 素材还没有 → 跳过，交回宿主
    this.action = item;
    var opts = null;
    if (item.name === 'released') {
      var af = this.manifest.clips[clip].airborneFrame;
      opts = { once: true, from: af || 1 };
    } else if (item.name === 'grabbed') {
      opts = null;                           // 用剪辑自身的 intro+loop 结构
    }
    this.playClip(clip, opts);
    return true;
  };

  /* 瞬时动作入口。
     front=true 表示"打断当前动作立刻播" —— 这是必须的：
     `drag` 剪辑是**无限循环**的，松手时如果不打断，
     `this.action` 永远不为空，排队的 `released`（下落+落地）就永远不会启动，
     角色会一直吊在半空。
     实测症状：拖起来之后松手，她一直悬着不落地。 */
  PetAnimation.prototype.trigger = function (name, front) {
    var item = { name: name, t: Date.now() };
    if (front) {
      this.queue.unshift(item);
      this.action = null;                    // 打断当前动作
      this.nextInQueue();
    } else {
      this.queue.push(item);
      if (!this.action) this.nextInQueue();
    }
    return item;
  };

  PetAnimation.prototype.clearActions = function () {
    this.queue.length = 0;
    this.action = null;
    this.applyState();
  };

  PetAnimation.prototype.currentClip = function () {
    return this.clipName;
  };

  global.PetAnimation = PetAnimation;
})(window);
