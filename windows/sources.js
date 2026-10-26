'use strict';
/*
 * 今日 token —— 三个来源分别读各自的本地日志。
 *
 * 和 macOS 版 usage_stats.py 同口径：
 *   WorkBuddy   ~/.workbuddy/projects/*\/*.jsonl   → providerData.rawUsage
 *   ChatGPT     ~/.codex/sessions/YYYY/MM/DD/*.jsonl → token_usage_record（按 response_id 去重）
 *   Claude Code ~/.claude/projects/**\/*.jsonl      → message.usage（按 message.id 去重）
 *
 * 在 Windows 上这些目录会解析到 %USERPROFILE%\.workbuddy 等位置。
 * 目录不存在就跳过该来源 —— 不报错、不编数字，前端会把缺失的来源显示成「—」。
 *
 * 性能：Codex 的历史目录能到几百 MB，但"今日"只需要今天的日期目录，
 * 所以这里只读当天（不像 Python 版为了生成历史报告要全量扫）。
 */

const fs = require('fs');
const fsp = fs.promises;
const os = require('os');
const path = require('path');

const { priceTable } = require('./pricing');

const WB_PROJECTS = path.join(os.homedir(), '.workbuddy', 'projects');
const CODEX_SESSIONS = path.join(os.homedir(), '.codex', 'sessions');
const CLAUDE_PROJECTS = (process.env.PET_CLAUDE_DIR
  ? process.env.PET_CLAUDE_DIR.replace(/^~(?=$|[\\/])/, os.homedir())
  : path.join(os.homedir(), '.claude', 'projects'));

const SOURCE_NAMES = ['workbuddy', 'codex', 'claude'];

// 判定窗口（毫秒）—— 与 claude-code.js / pet_sources.py 保持一致，
// 三个实现必须同值，否则同一个会话在 mac 和 win 上会给出不同状态
const FRESH_MS = 4000;
const WORK_WINDOW_MS = 25000;
const WAITING_MS = 45000;
const SLEEP_MS = 60000;
const SCAN_WINDOW_MS = 180000;

// path -> { mtime, size, rows }
const fileCache = new Map();

function pad(n) {
  return String(n).padStart(2, '0');
}

function sameDay(ts, now) {
  const d = new Date(ts);
  return d.getFullYear() === now.getFullYear()
    && d.getMonth() === now.getMonth()
    && d.getDate() === now.getDate();
}

async function walk(dir, depth, out = []) {
  if (depth < 0) return out;
  let entries;
  try {
    entries = await fsp.readdir(dir, { withFileTypes: true });
  } catch (err) {
    return out;
  }
  for (const ent of entries) {
    const full = path.join(dir, ent.name);
    if (ent.isDirectory()) {
      if (depth > 0) await walk(full, depth - 1, out);
    } else if (ent.isFile() && ent.name.endsWith('.jsonl')) {
      out.push(full);
    }
  }
  return out;
}

/**
 * 找出「今天被写过」的 rollout 文件。
 *
 * ⚠️ 两个坑都踩过：
 *   1. 不能只读 sessions/<今天>/：rollout 文件按**会话开始日**建，一个持续几天的会话
 *      会把今天的记录追加在几天前的目录里 —— 实测这里漏了 21M token。
 *   2. 不能按目录 mtime 剪枝：往文件里追加内容只更新**文件** mtime，
 *      父目录的 mtime 不动，剪枝会把整棵目录误判成"没动过"。
 * 所以老老实实遍历 年/月/日 三层，逐个 stat 文件。几千次 stat 在本地盘上是毫秒级，
 * 真正贵的是解析，而解析有 mtime+size 缓存兜着。
 */
async function codexFilesTouched(since) {
  const out = [];
  const names = async (dir) => {
    try {
      return await fsp.readdir(dir);
    } catch (err) {
      return [];
    }
  };
  for (const y of await names(CODEX_SESSIONS)) {
    const yDir = path.join(CODEX_SESSIONS, y);
    for (const m of await names(yDir)) {
      const mDir = path.join(yDir, m);
      for (const d of await names(mDir)) {
        const dDir = path.join(mDir, d);
        for (const f of await names(dDir)) {
          if (!f.endsWith('.jsonl')) continue;
          const full = path.join(dDir, f);
          try {
            const st = await fsp.stat(full);
            if (st.mtimeMs >= since) out.push(full);
          } catch (err) { /* 刚被删 */ }
        }
      }
    }
  }
  return out;
}

/** 读文件并按 mtime+size 缓存解析结果 —— 轮询每 3 秒一次，不能每次重解析 */
async function cachedParse(file, parser) {
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
  let rows = [];
  try {
    rows = parser(text);
  } catch (err) {
    rows = [];
  }
  fileCache.set(file, { mtime: stat.mtimeMs, size: stat.size, rows });
  return rows;
}

/* ---------------- WorkBuddy ---------------- */

function parseWorkbuddy(text) {
  const rows = [];
  for (const line of text.split('\n')) {
    if (!line.includes('rawUsage')) continue;
    let rec;
    try {
      rec = JSON.parse(line);
    } catch (err) {
      continue;
    }
    const pd = rec.providerData || {};
    const usage = pd.rawUsage;
    if (!usage) continue;
    const ts = Number(rec.timestamp) || 0;
    if (!ts) continue;
    const prompt = Number(usage.prompt_tokens) || 0;
    const completion = Number(usage.completion_tokens) || 0;
    const total = Number(usage.total_tokens) || (prompt + completion);
    rows.push({ ts, prompt, completion, total, model: String(pd.requestModelName || pd.model || '') });
  }
  return rows;
}

async function workbuddyToday(now) {
  const files = await walk(WB_PROJECTS, 1);   // projects/<项目>/<会话>.jsonl
  const seen = new Set();
  const modelCalls = new Map();
  let tokens = 0;
  for (const file of files) {
    const rows = await cachedParse(file, parseWorkbuddy);
    for (const row of rows) {
      if (!sameDay(row.ts, now)) continue;
      // 防重放：同毫秒 + 同用量 + 同模型 视为同一次（跨文件）
      const key = `${row.ts}|${row.prompt}|${row.completion}|${row.model}`;
      if (seen.has(key)) continue;
      seen.add(key);
      tokens += row.total;
      const m = row.model || '未知模型';
      modelCalls.set(m, (modelCalls.get(m) || 0) + 1);
    }
  }
  return { tokens, calls: seen.size, models: [...modelCalls.keys()] };
}

/* ---------------- ChatGPT（程序内部叫 Codex） ---------------- */

function parseCodex(text) {
  const turns = new Map();
  const records = [];
  for (const line of text.split('\n')) {
    if (!line.includes('token_usage_record') && !line.includes('turn_context')) continue;
    let rec;
    try {
      rec = JSON.parse(line);
    } catch (err) {
      continue;
    }
    const payload = rec.payload || {};
    if (rec.type === 'turn_context') {
      if (payload.turn_id) turns.set(payload.turn_id, payload.model || '');
    } else if (rec.type === 'token_usage_record') {
      records.push({ ts: Date.parse(rec.timestamp || ''), payload });
    }
  }
  return { turns, records };
}

async function codexToday(now) {
  const startOfDay = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const files = await codexFilesTouched(startOfDay);

  const seen = new Set();
  const models = new Set();
  let tokens = 0;
  for (const file of files) {
    const { turns, records } = await cachedParse(file, parseCodex);
    for (const { ts, payload } of records) {
      if (!Number.isFinite(ts) || !sameDay(ts, now)) continue;
      const rid = payload.response_id;
      if (rid) {
        if (seen.has(rid)) continue;
        seen.add(rid);
      } else {
        // 没有 response_id 时用「会话 + 时间 + 用量」兜底，避免静默重复
        const fallback = JSON.stringify([payload.thread_id || payload.session_id,
          ts, payload.turn_id, payload.usage || {}]);
        if (seen.has(fallback)) continue;
        seen.add(fallback);
      }
      const usage = payload.usage || {};
      const prompt = Number(usage.input_tokens) || 0;
      const completion = Number(usage.output_tokens) || 0;
      tokens += Number(usage.total_tokens) || (prompt + completion);
      const model = turns.get(payload.turn_id);
      if (model) models.add(model);
    }
  }
  return { tokens, calls: seen.size, models: [...models] };
}

/* ---------------- Claude Code ---------------- */

async function claudeToday(now) {
  // 复用 claude-code.js 的解析（它已经按 message.id 去重、只保留 Claude 模型）
  // eslint-disable-next-line global-require
  const cc = require('./claude-code');
  const snap = await cc.costSnapshot(now);
  return { tokens: snap.tokens.today, calls: snap.todayRequests, models: [] };
}

/* ---------------- WorkBuddy 的状态（心跳 + 日志） ---------------- */

const WB_SESSIONS = path.join(os.homedir(), '.workbuddy', 'sessions');

const WB_LABELS = {
  idle: '空闲', thinking: '思考中', working: '执行中',
  success: '刚完成', waiting: '等你回复', error: '出错了',
  offline: '未连接', sleeping: '睡着啦',
};

/** 读日志尾部判断"最后在干什么" —— 窗口值与 macOS 的 pet_sources.py 一致 */
function workbuddyTailState(file, now) {
  const lines = [];  // 同步读尾部，调用方已经保证文件不大
  let buf;
  try {
    const stat = fs.statSync(file);
    const start = Math.max(0, stat.size - 200000);
    const fd = fs.openSync(file, 'r');
    buf = Buffer.alloc(stat.size - start);
    fs.readSync(fd, buf, 0, buf.length, start);
    fs.closeSync(fd);
  } catch (err) {
    return null;
  }
  let text = buf.toString('utf8');
  const arr = text.split('\n');
  if (arr[0] && !arr[0].startsWith('{')) arr.shift();   // 首行可能被截断
  for (const line of arr) if (line) lines.push(line);

  for (let i = lines.length - 1; i >= 0; i -= 1) {
    let rec;
    try {
      rec = JSON.parse(lines[i]);
    } catch (err) {
      continue;
    }
    const ts = Number(rec.timestamp) || 0;
    if (!ts) continue;
    const age = now.getTime() - ts;
    if (age > SCAN_WINDOW_MS) return { state: 'idle', ts };
    const type = rec.type;
    if (type === 'function_call') return { state: age < 25000 ? 'working' : 'idle', ts };
    if (type === 'function_call_result') return { state: age < 4000 ? 'working' : 'thinking', ts };
    if (type === 'reasoning') {
      if (age < 4000) return { state: 'thinking', ts };
      continue;                              // 推理记录很久没更新不代表状态变了
    }
    if (type === 'message') {
      if (rec.role === 'user') return { state: age < 45000 ? 'thinking' : 'idle', ts };
      if (age < 25000) return { state: 'success', ts };
      if (age < 45000) return { state: 'waiting', ts };
      return { state: 'idle', ts };
    }
  }
  return null;
}

/**
 * WorkBuddy 的会话状态。
 *
 * ⚠️ 两个证据缺一不可：
 *   ① 心跳新鲜 = 进程还开着（sessions/*.json 的 lastHeartbeat）
 *   ② 日志刚被写 = 真的在干活（projects/*\/*.jsonl 的 mtime）
 * 实测 macOS 上心跳**只在会话启动时写一次**，一分钟后必然"过期"；
 * 只认心跳会把正在干活的会话踢掉，去读一个七天没活动的僵尸会话。
 */
async function workbuddyState(now = new Date()) {
  let sessionNames;
  try {
    sessionNames = await fsp.readdir(WB_SESSIONS);
  } catch (err) {
    return null;
  }
  const logFiles = await walk(WB_PROJECTS, 1);
  const bySid = new Map();
  for (const file of logFiles) bySid.set(path.basename(file, '.jsonl'), file);

  const cands = [];
  for (const name of sessionNames) {
    if (!name.endsWith('.json')) continue;
    let raw;
    try {
      raw = JSON.parse(await fsp.readFile(path.join(WB_SESSIONS, name), 'utf8'));
    } catch (err) {
      continue;
    }
    const sid = raw.sessionId;
    if (!sid || typeof sid !== 'string') continue;
    const hb = Number(raw.lastHeartbeat) || 0;
    const logFile = bySid.get(sid);
    if (!logFile) continue;
    let mtime = 0;
    try {
      mtime = (await fsp.stat(logFile)).mtimeMs;
    } catch (err) {
      continue;
    }
    const hbFresh = hb > 0 && now.getTime() - hb < SLEEP_MS;
    const logFresh = now.getTime() - mtime < SLEEP_MS;
    if (!hbFresh && !logFresh) continue;
    cands.push({ sid, hb, mtime, file: logFile, cwd: raw.cwd || null });
  }
  if (!cands.length) return null;

  const priority = { working: 0, thinking: 1, error: 2, waiting: 3, success: 4, idle: 5, sleeping: 6 };
  let best = null;
  for (const cand of cands) {
    const tail = workbuddyTailState(cand.file, now);
    const state = tail ? tail.state : 'idle';
    const lastActivity = tail ? tail.ts : Math.round(cand.mtime);
    const score = [priority[state] ?? 9, -Math.max(lastActivity, cand.mtime)];
    if (!best || score[0] < best.score[0]
      || (score[0] === best.score[0] && score[1] < best.score[1])) {
      best = {
        state, score, sid: cand.sid, cwd: cand.cwd,
        lastActivity: Math.round(Math.max(lastActivity, cand.hb)),
      };
    }
  }
  return {
    state: best.state, label: WB_LABELS[best.state] || best.state,
    task: null, cwd: best.cwd, sessionId: best.sid,
    lastActivity: best.lastActivity, activeSessions: cands.length,
    source: 'workbuddy',
  };
}

/* ---------------- 汇总 ---------------- */

/**
 * 合并后的状态：WorkBuddy 与 Claude Code 各取一份，选"最活跃 + 最后动过"的那个。
 * 排序规则照 macOS 的 AutoSource：先看谁在干活，再看谁最后动过。
 */
async function stateSnapshot(now = new Date()) {
  const ACTIVE = ['working', 'thinking', 'error', 'waiting'];
  // eslint-disable-next-line global-require
  const cc = require('./claude-code');
  const [wb, cl] = await Promise.all([
    workbuddyState(now).catch(() => null),
    cc.stateSnapshot(now).catch(() => null),
  ]);
  const cands = [wb, cl].filter(Boolean);
  if (!cands.length) {
    return {
      state: 'offline', label: '未连接', task: null, cwd: null, sessionId: null,
      lastActivity: null, activeSessions: 0, source: 'none',
    };
  }
  cands.sort((a, b) => {
    const ra = ACTIVE.includes(a.state) ? 0 : 1;
    const rb = ACTIVE.includes(b.state) ? 0 : 1;
    if (ra !== rb) return ra - rb;
    return (b.lastActivity || 0) - (a.lastActivity || 0);
  });
  return cands[0];
}

async function usageSnapshot(now = new Date()) {
  const results = await Promise.all([
    workbuddyToday(now).catch(() => ({ tokens: 0, calls: 0, models: [] })),
    codexToday(now).catch(() => ({ tokens: 0, calls: 0, models: [] })),
    claudeToday(now).catch(() => ({ tokens: 0, calls: 0, models: [] })),
  ]);
  const bySource = {};
  SOURCE_NAMES.forEach((name, i) => {
    // 目录不存在时给空对象而不是 0：0 会和"真的没用"混淆
    bySource[name] = results[i].tokens > 0 ? { totalTokens: results[i].tokens } : {};
  });
  const total = results.reduce((sum, r) => sum + r.tokens, 0);

  const missing = [];
  if (!fs.existsSync(WB_PROJECTS)) missing.push('WorkBuddy');
  if (!fs.existsSync(CODEX_SESSIONS)) missing.push('ChatGPT');
  if (!fs.existsSync(CLAUDE_PROJECTS)) missing.push('Claude Code');

  return {
    today: {
      dayLocal: `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`,
      totalTokens: total,
      bySource,
      complete: missing.length === 0,
      note: missing.length
        ? `本机没找到 ${missing.join(' / ')} 的日志目录，这部分没有统计`
        : '按本机自然日统计，三个来源都已接入',
    },
  };
}

module.exports = {
  WB_PROJECTS, CODEX_SESSIONS, CLAUDE_PROJECTS,
  parseWorkbuddy, parseCodex,
  workbuddyToday, codexToday, claudeToday,
  workbuddyState, workbuddyTailState,
  usageSnapshot, stateSnapshot,
};
