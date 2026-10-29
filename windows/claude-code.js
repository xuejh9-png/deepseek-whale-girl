'use strict';
/*
 * Claude Code 数据层 —— Windows 版的全部数据来源。
 *
 * 为什么不用 Python 那边现成的 pet_daemon.py：
 *   Windows 用户装完 exe 就能用，不该再被要求装 Python。
 *   所以这里用 Node 重写「读日志 → 状态 / 今日 token / 花费」这一段，
 *   口径和 macOS 版逐项对齐（同样的去重规则、同样的价目表、同样的时间窗口）。
 *
 * WorkBuddy / ChatGPT 的汇总见 sources.js；中转站实扣见 provider-usage.js。
 */

const fs = require('fs');
const fsp = fs.promises;
const os = require('os');
const path = require('path');

const { priceTable, priceOf, pricingPath, PRICING_ENV } = require('./pricing');

const CLAUDE_PROJECTS = (process.env.PET_CLAUDE_DIR
  ? process.env.PET_CLAUDE_DIR.replace(/^~(?=$|[\\/])/, os.homedir())
  : path.join(process.env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), '.claude'), 'projects'));

// 判定窗口（毫秒）—— 与 usage_stats.py / pet_sources.py 保持一致
const SUCCESS_WINDOW_MS = 25000;
const WAITING_MS = 45000;
const SLEEP_MS = 60000;

const LABELS = {
  idle: '空闲', thinking: '思考中', working: '执行中',
  success: '刚完成', waiting: '等你回复', error: '出错了',
  offline: '未连接', sleeping: '睡着啦',
};

// path -> { mtime, size, rows }：按 mtime+size 增量，避免每次轮询重扫几十 MB
const fileCache = new Map();

function isClaudeModel(model) {
  return typeof model === 'string'
    && /(?:^|[/.:])claude-(?:opus|sonnet|haiku|[0-9])/i.test(model);
}

function num(value) {
  return Number.isFinite(Number(value)) ? Number(value) : null;
}

/**
 * 读一个 token 分量。**必须和 usage_stats.py 的 _tokens() 同语义**，
 * 否则同一份 Claude 日志在 mac 和 win 上会算出两个数（2026-10-26 实测翻车）。
 *
 *   Python: usage.get(k, 0) → 字段缺失按 0 算；字段存在但不是 int → 整条跳过
 *   这里:   字段缺失 / null → 0；存在但解析成 NaN 或负数 → null（整条跳过）
 *
 * 反过来的代价很大：原来缺失即跳过，某些客户端（实测 CherryStudio 那条链）
 * 写的 assistant 记录不带 cache_creation_input_tokens，于是整条调用被丢掉，
 * 「今日 Token」比接口账单少一大截，用户只会认定我们算错了。
 */
function pickCount(usage, key) {
  if (!usage || !(key in usage)) return 0;
  const raw = usage[key];
  if (raw === null || raw === undefined) return 0;
  const n = Number(raw);
  return Number.isFinite(n) && n >= 0 ? n : null;
}

async function walk(dir, out = []) {
  let entries;
  try {
    entries = await fsp.readdir(dir, { withFileTypes: true });
  } catch (err) {
    return out;
  }
  for (const ent of entries) {
    const full = path.join(dir, ent.name);
    if (ent.isDirectory()) await walk(full, out);
    else if (ent.isFile() && ent.name.endsWith('.jsonl')) out.push(full);
  }
  return out;
}

/** 单个会话文件的 usage 记录（有缓存） */
async function parseFile(file) {
  let stat;
  try {
    stat = await fsp.stat(file);
  } catch (err) {
    return [];
  }
  const hit = fileCache.get(file);
  if (hit && hit.mtime === stat.mtimeMs && hit.size === stat.size) return hit.rows;

  let text;
  try {
    text = await fsp.readFile(file, 'utf8');
  } catch (err) {
    return [];
  }
  const rows = [];
  for (const line of text.split('\n')) {
    if (!line.includes('"usage"')) continue;
    let rec;
    try {
      rec = JSON.parse(line);
    } catch (err) {
      continue;
    }
    if (rec.type !== 'assistant') continue;
    const msg = rec.message || {};
    const usage = msg.usage || {};
    const model = String(msg.model || '');
    if (!isClaudeModel(model) || !rec.timestamp) continue;
    const counts = [
      pickCount(usage, 'input_tokens'), pickCount(usage, 'output_tokens'),
      pickCount(usage, 'cache_creation_input_tokens'), pickCount(usage, 'cache_read_input_tokens'),
    ];
    // 只有「字段存在但值不合法」才整条跳过；缺失的分量按 0 计（与 Python 版一致）
    if (counts.some((n) => n === null)) continue;
    const identity = msg.id || rec.requestId;
    // 没有稳定 ID 时，流式重复和多次请求无法区分 —— 不猜
    if (!identity) continue;
    const ts = Date.parse(rec.timestamp);
    if (!Number.isFinite(ts)) continue;
    rows.push({
      id: String(identity), model, ts, counts,
      sessionId: String(rec.sessionId || identity),
      cwd: String(rec.cwd || 'Claude Code'),
    });
  }
  fileCache.set(file, { mtime: stat.mtimeMs, size: stat.size, rows });
  return rows;
}

/** 全部调用记录；同一 message id 取各分量最大值（流式消息是累加的） */
async function collect() {
  const files = await walk(CLAUDE_PROJECTS);
  const byId = new Map();
  for (const file of files) {
    const rows = await parseFile(file);
    for (const row of rows) {
      const old = byId.get(row.id);
      if (old) {
        old.counts = old.counts.map((n, i) => Math.max(n, row.counts[i]));
        old.ts = Math.min(old.ts, row.ts);   // 归到这次请求的第一次用量
      } else {
        byId.set(row.id, { ...row, counts: row.counts.slice() });
      }
    }
  }
  return [...byId.values()];
}

function sameDay(a, b) {
  return a.getFullYear() === b.getFullYear()
    && a.getMonth() === b.getMonth()
    && a.getDate() === b.getDate();
}

function dayKey(d) {
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/** 花费快照 —— 结构与 macOS 版 /claude-cost 完全一致，前端不用改 */
async function costSnapshot(now = new Date()) {
  const table = priceTable();
  const rows = await collect();
  let total = 0;
  let today = 0;
  let tokens = 0;
  let todayTokens = 0;
  let calls = 0;
  let todayCalls = 0;
  let unpriced = 0;
  let first = null;
  let last = null;

  for (const row of rows) {
    const [inp, out, creation, read] = row.counts;
    const rowTokens = inp + out + creation + read;
    const when = new Date(row.ts);
    const isToday = sameDay(when, now);

    // ⚠️ token 与调用次数不依赖价目表：模型没进价目表也要如实计数，
    // 否则「今日 Token」会莫名少一块，用户只会觉得数字不准。
    tokens += rowTokens;
    calls += 1;
    if (isToday) {
      todayTokens += rowTokens;
      todayCalls += 1;
    }
    if (!first || row.ts < first.ts) first = { ts: row.ts, when };
    if (!last || row.ts > last.ts) last = { ts: row.ts, when };

    const price = priceOf(row.model, table);
    if (!price) {
      unpriced += 1;
      continue;
    }
    const amount = (inp * price[0] + out * price[1] + read * price[2] + creation * price[3]) / 1e6;
    total += amount;
    if (isToday) today += amount;
  }

  const round = (n) => Math.round(n * 1e4) / 1e4;
  const hasOwnPricing = fs.existsSync(pricingPath());
  return {
    amount: round(total),
    currency: 'USD',
    verified: true,
    estimated: true,
    source: 'Claude Code 本地日志 × 模型单价',
    updatedAt: now.toISOString(),
    coverageStart: first ? first.when.toISOString() : null,
    coverageEnd: last ? last.when.toISOString() : null,
    note: hasOwnPricing
      ? '按模型单价折算（与 CC Switch 用量面板同口径），不是中转站账单'
      : `按模型单价折算（价目来源：内置公开价目，可设 ${PRICING_ENV} 指定自己的表），不是中转站账单`,
    byCurrency: { USD: String(round(total)) },
    today: { USD: String(round(today)) },
    month: { USD: String(round(total)) },
    requests: calls,
    todayRequests: todayCalls,
    // 有多少次调用因为模型不在价目表里而没算进金额（token 仍然算进去了）
    unpricedRequests: unpriced,
    tokens: { total: tokens, today: todayTokens },
  };
}

/** 读文件末尾若干行（只为了看"最后发生了什么"，不需要全文） */
async function readTail(file, maxLines = 200, maxBytes = 600000) {
  let handle;
  try {
    handle = await fsp.open(file, 'r');
    const stat = await handle.stat();
    const start = Math.max(0, stat.size - maxBytes);
    const length = stat.size - start;
    const buf = Buffer.alloc(length);
    await handle.read(buf, 0, length, start);
    const lines = buf.toString('utf8').split('\n');
    if (start > 0) lines.shift();              // 首行可能被截断
    return lines.filter(Boolean).slice(-maxLines);
  } catch (err) {
    return [];
  } finally {
    if (handle) await handle.close().catch(() => {});
  }
}

// 只认业务事件。带时间戳的 attachment / stop_hook_summary 也不能覆盖任务状态。
function eventState(rec) {
  if (rec.isSidechain) return null;
  const msg = rec.message || {};
  const parts = Array.isArray(msg.content) ? msg.content : [];
  const has = (type) => parts.some((p) => p && p.type === type);
  if (rec.type === 'assistant') {
    if (has('tool_use') || msg.stop_reason === 'tool_use') return 'working';
    if (msg.stop_reason === 'end_turn' || msg.stop_reason === 'stop_sequence') return 'success';
    if (has('thinking') && !has('text')) return 'thinking';
    // 旧版日志不写 stop_reason；显式 null 表示流式生成尚未结束。
    if (has('text')) return msg.stop_reason === null ? 'thinking' : 'success';
  }
  if (rec.type === 'user') {
    if (has('tool_result')) return 'thinking';
    if (typeof msg.content === 'string' && msg.content.startsWith('[Request interrupted by user')) return 'error';
    if (typeof msg.content === 'string' || has('text')) return 'thinking';
  }
  if (rec.type === 'api-request') return 'thinking';
  if (rec.type === 'progress') return 'working';
  if (rec.type === 'system' && rec.subtype === 'api_error') return 'error';
  return null;
}

function scanTail(lines) {
  const recs = [];
  for (const line of lines) {
    try { recs.push(JSON.parse(line)); } catch (err) { /* 正在追加的半行 */ }
  }
  let cwd = null, sessionId = null;
  for (const rec of recs) {
    if (rec.cwd) cwd = String(rec.cwd);
    if (rec.sessionId) sessionId = String(rec.sessionId);
  }
  for (let i = recs.length - 1; i >= 0; i -= 1) {
    const rec = recs[i];
    const ts = Date.parse(rec.timestamp || '');
    if (!Number.isFinite(ts)) continue;
    const state = eventState(rec);
    if (state) return { ts, state, cwd, sessionId };
  }
  return null;
}

// 日志没有可靠的进程心跳；半小时无业务事件视为陈旧，避免崩溃后永久工作。
const ACTIVE_STALE_MS = 30 * 60 * 1000;
const stateCache = new Map();

/** 状态推断 —— 取最近真的写过业务记录的那个会话
 *
 * ⚠️ 不能只看 mtime 最新的文件：`~/.claude/projects` 下可能有
 * 「只写了 last-prompt / cost-state 这类元数据」的临时会话，mtime 很新、
 * 却一条带时间的业务记录都没有。按 mtime 选会挑到它，状态就永远停在 idle。
 */
async function stateSnapshot(now = new Date()) {
  const blank = (state, extra = {}) => ({
    state, label: LABELS[state] || state, task: null, cwd: null,
    sessionId: null, lastActivity: null, activeSessions: 0,
    source: 'claude', ...extra,
  });

  const files = (await walk(CLAUDE_PROJECTS)).filter((file) => {
    // 子代理日志不代表主会话；否则主任务完成后子代理残留会把动画拉回工作。
    return path.relative(CLAUDE_PROJECTS, file).split(path.sep).length === 2;
  });
  if (!files.length) return blank('offline');

  const stats = [];
  for (const file of files) {
    try {
      const st = await fsp.stat(file);
      stats.push({ file, mtime: st.mtimeMs, size: st.size });
    } catch (err) { /* 文件刚被删 */ }
  }
  if (!stats.length) return blank('offline');
  stats.sort((a, b) => b.mtime - a.mtime);

  // 不限前三个：元数据空会话再多也不能遮住真正的业务会话。
  let best = null;
  const present = new Set(files);
  for (const file of stateCache.keys()) if (!present.has(file)) stateCache.delete(file);
  for (const stat of stats) {
    let cached = stateCache.get(stat.file);
    if (!cached || cached.mtime !== stat.mtime || cached.size !== stat.size) {
      cached = { ...stat, info: scanTail(await readTail(stat.file, 200, 120000)) };
      stateCache.set(stat.file, cached);
    }
    const info = cached.info;
    if (info && (!best || info.ts > best.ts)) best = info;
  }
  if (!best) {
    // 有日志文件，但读不到任何带时间的记录 —— 不猜
    return blank('offline', { lastActivity: Math.round(stats[0].mtime) });
  }

  const age = now.getTime() - best.ts;
  let state = best.state;
  if (state === 'working' || state === 'thinking') {
    if (age >= ACTIVE_STALE_MS) state = 'sleeping';
  } else if (age >= SLEEP_MS) state = 'sleeping';
  else if (state === 'success' && age >= WAITING_MS) state = 'idle';
  else if (state === 'success' && age >= SUCCESS_WINDOW_MS) state = 'waiting';
  return blank(state, {
    cwd: best.cwd, sessionId: best.sessionId,
    lastActivity: Math.round(best.ts), activeSessions: 1,
  });
}

module.exports = {
  CLAUDE_PROJECTS, LABELS,
  isClaudeModel, collect, costSnapshot, stateSnapshot,
};
