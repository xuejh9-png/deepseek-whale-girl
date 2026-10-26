'use strict';
/*
 * Claude Code 数据层 —— Windows 版的全部数据来源。
 *
 * 为什么不用 Python 那边现成的 pet_daemon.py：
 *   Windows 用户装完 exe 就能用，不该再被要求装 Python。
 *   所以这里用 Node 重写「读日志 → 状态 / 今日 token / 花费」这一段，
 *   口径和 macOS 版逐项对齐（同样的去重规则、同样的价目表、同样的时间窗口）。
 *
 * 只接 Claude Code：WorkBuddy / ChatGPT 的解析逻辑重写成本高，
 * 而且它们都不是 Windows 上的主力工具。这一点在 README 里明确标注。
 */

const fs = require('fs');
const fsp = fs.promises;
const os = require('os');
const path = require('path');

const { priceTable, priceOf, pricingPath, PRICING_ENV } = require('./pricing');

const CLAUDE_PROJECTS = (process.env.PET_CLAUDE_DIR
  ? process.env.PET_CLAUDE_DIR.replace(/^~(?=$|[\\/])/, os.homedir())
  : path.join(os.homedir(), '.claude', 'projects'));

// 判定窗口（毫秒）—— 与 usage_stats.py / pet_sources.py 保持一致
const FRESH_MS = 4000;
const WORK_WINDOW_MS = 25000;
const SUCCESS_WINDOW_MS = 25000;
const WAITING_MS = 45000;
const SLEEP_MS = 60000;
const SCAN_WINDOW_MS = 180000;

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
      num(usage.input_tokens), num(usage.output_tokens),
      num(usage.cache_creation_input_tokens), num(usage.cache_read_input_tokens),
    ];
    // 缺任何一个分量就没法按四条分量计价 —— 不猜，跳过
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
  let first = null;
  let last = null;

  for (const row of rows) {
    const price = priceOf(row.model, table);
    if (!price) continue;
    const [inp, out, creation, read] = row.counts;
    const amount = (inp * price[0] + out * price[1] + read * price[2] + creation * price[3]) / 1e6;
    const when = new Date(row.ts);
    total += amount;
    calls += 1;
    tokens += inp + out + creation + read;
    if (sameDay(when, now)) {
      today += amount;
      todayCalls += 1;
      todayTokens += inp + out + creation + read;
    }
    if (!first || row.ts < first.ts) first = { ts: row.ts, when };
    if (!last || row.ts > last.ts) last = { ts: row.ts, when };
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

/** 从一段尾部行里取出「最后一条有时间的记录」—— 没有就返回 null */
function scanTail(lines) {
  let cwd = null;
  let sessionId = null;
  // 元信息先补：日志末尾常有 summary / cost-state 这类没有时间戳的记录
  for (let i = lines.length - 1; i >= 0 && (!cwd || !sessionId); i -= 1) {
    try {
      const rec = JSON.parse(lines[i]);
      if (!cwd && rec.cwd) cwd = String(rec.cwd);
      if (!sessionId && rec.sessionId) sessionId = String(rec.sessionId);
    } catch (err) { /* 首行可能被截断 */ }
  }
  for (let i = lines.length - 1; i >= 0; i -= 1) {
    let rec;
    try {
      rec = JSON.parse(lines[i]);
    } catch (err) {
      continue;
    }
    const ts = Date.parse(rec.timestamp || '');
    if (!Number.isFinite(ts)) continue;
    let kind = rec.type;
    if (rec.type === 'assistant') {
      const content = (rec.message || {}).content;
      const toolUse = Array.isArray(content)
        && content.some((part) => part && part.type === 'tool_use');
      kind = toolUse ? 'assistant-tool' : 'assistant-text';
    }
    return { ts, kind, cwd, sessionId };
  }
  return null;
}

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

  const files = await walk(CLAUDE_PROJECTS);
  if (!files.length) return blank('offline');

  const stats = [];
  for (const file of files) {
    try {
      stats.push({ file, mtime: (await fsp.stat(file)).mtimeMs });
    } catch (err) { /* 文件刚被删 */ }
  }
  if (!stats.length) return blank('offline');
  stats.sort((a, b) => b.mtime - a.mtime);

  // 只看最新的几个：日志目录可能积了几百个会话，全读没必要
  let best = null;
  for (const stat of stats.slice(0, 3)) {
    const info = scanTail(await readTail(stat.file, 200, 120000));
    if (info && (!best || info.ts > best.ts)) best = info;
  }
  if (!best) {
    // 有日志文件，但读不到任何带时间的记录 —— 不猜
    return blank('offline', { lastActivity: Math.round(stats[0].mtime) });
  }

  const age = now.getTime() - best.ts;
  let state = 'idle';
  if (age <= SCAN_WINDOW_MS) {
    if (best.kind === 'assistant-tool') state = age < WORK_WINDOW_MS ? 'working' : 'idle';
    else if (best.kind === 'assistant-text') {
      if (age < SUCCESS_WINDOW_MS) state = 'success';
      else if (age < WAITING_MS) state = 'waiting';
      else state = 'idle';
    } else if (best.kind === 'user') {
      // 用户刚发完言 → 球在模型这边，正在生成
      state = age < WAITING_MS ? 'thinking' : 'idle';
    } else {
      state = 'idle';
    }
  }

  // 球不在模型手里、且安静够久 → 睡着（working / thinking 不睡）
  if (['success', 'waiting', 'idle'].includes(state) && age >= SLEEP_MS) {
    state = 'sleeping';
  }
  return blank(state, {
    cwd: best.cwd, sessionId: best.sessionId,
    lastActivity: Math.round(best.ts), activeSessions: 1,
  });
}

module.exports = {
  CLAUDE_PROJECTS, LABELS,
  isClaudeModel, collect, costSnapshot, stateSnapshot,
};
