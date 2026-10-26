'use strict';
/*
 * Claude 日志诊断 —— 回答一个问题：本地日志里到底有多少条调用，
 * 我们为什么只算出了其中一部分。
 *
 * 背景（2026-10-26）：
 *   Windows 上「今日 Token」83.28M，而接口账单 99.4M；差额几乎全在 Claude
 *   （本地 30 次 / 16.47M，账单 190 次 / 32.4M）。要么日志分散在别处，
 *   要么解析把记录丢了。这个脚本把「丢在哪一步」逐条数出来，不做任何猜测。
 *
 * 用法（Windows PowerShell 或 CMD 都行）：
 *   cd %USERPROFILE%\Desktop\WorkBuddy用量\windows
 *   node diagnose-claude.js
 *
 * 只读日志，不改任何文件。
 */

const fs = require('fs');
const fsp = fs.promises;
const path = require('path');

const { CLAUDE_PROJECTS, isClaudeModel } = require('./claude-code');

function sameDay(ts, now) {
  const d = new Date(ts);
  return d.getFullYear() === now.getFullYear()
    && d.getMonth() === now.getMonth()
    && d.getDate() === now.getDate();
}

function pick(usage, key) {
  if (!usage || !(key in usage)) return 0;
  const raw = usage[key];
  if (raw === null || raw === undefined) return 0;
  const n = Number(raw);
  return Number.isFinite(n) && n >= 0 ? n : null;
}

function comma(n) {
  return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
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

async function main() {
  const now = new Date();
  console.log('');
  console.log('Claude 日志诊断  ' + now.toISOString().slice(0, 19).replace('T', ' '));
  console.log('日志目录：' + CLAUDE_PROJECTS);
  if (!fs.existsSync(CLAUDE_PROJECTS)) {
    console.log('❌ 这个目录不存在 —— Windows 上根本没有 Claude Code 的日志。');
    console.log('   如果 Claude 是在别的客户端里跑的，它的用量不在本地文件里，');
    console.log('   本机统计永远对不上接口账单，只能以账单为准。');
    return;
  }

  const files = await walk(CLAUDE_PROJECTS);
  const rows = {
    total: 0, ok: 0, okTokens: 0,
    notModel: 0, noId: 0, badCount: 0, noTs: 0, notAssistant: 0, notToday: 0,
  };
  const models = new Map();
  const badModels = new Map();
  const todayFileHits = [];

  for (const file of files) {
    let text;
    try {
      text = await fsp.readFile(file, 'utf8');
    } catch (err) {
      continue;
    }
    let fileToday = 0;
    for (const line of text.split('\n')) {
      if (!line || !line.includes('"usage"')) continue;
      let rec;
      try {
        rec = JSON.parse(line);
      } catch (err) {
        continue;
      }
      const msg = rec.message || {};
      const usage = msg.usage || {};
      if (!usage || !Object.keys(usage).length) continue;

      if (rec.type !== 'assistant') { rows.notAssistant += 1; continue; }
      rows.total += 1;

      const ts = Date.parse(rec.timestamp || '');
      if (!Number.isFinite(ts)) { rows.noTs += 1; continue; }
      if (!sameDay(ts, now)) { rows.notToday += 1; continue; }
      fileToday += 1;

      const model = String(msg.model || '');
      if (!isClaudeModel(model)) {
        rows.notModel += 1;
        badModels.set(model, (badModels.get(model) || 0) + 1);
        continue;
      }
      const counts = [
        pick(usage, 'input_tokens'), pick(usage, 'output_tokens'),
        pick(usage, 'cache_creation_input_tokens'), pick(usage, 'cache_read_input_tokens'),
      ];
      if (counts.some((n) => n === null)) { rows.badCount += 1; continue; }
      const identity = msg.id || rec.requestId;
      if (!identity) { rows.noId += 1; continue; }

      const tok = counts[0] + counts[1] + counts[2] + counts[3];
      rows.ok += 1;
      rows.okTokens += tok;
      const key = model + '  [' + counts.map(comma).join(' + ') + ']';
      models.set(key, (models.get(key) || 0) + 1);
    }
    if (fileToday) todayFileHits.push({ file, n: fileToday });
  }

  console.log('会话文件总数：' + files.length);
  console.log('今天有记录的会话文件：' + todayFileHits.length);
  console.log('');
  console.log('—— 今天的 assistant 记录 ——');
  console.log('  计入统计        ' + String(rows.ok).padStart(6) + ' 条   ' + comma(rows.okTokens) + ' token');
  console.log('  模型名不匹配    ' + String(rows.notModel).padStart(6) + ' 条');
  console.log('  缺稳定 id       ' + String(rows.noId).padStart(6) + ' 条');
  console.log('  用量字段是脏值  ' + String(rows.badCount).padStart(6) + ' 条');
  console.log('  没有时间戳      ' + String(rows.noTs).padStart(6) + ' 条');
  console.log('  不是今天        ' + String(rows.notToday).padStart(6) + ' 条（历史记录，正常）');
  console.log('  非 assistant    ' + String(rows.notAssistant).padStart(6) + ' 条（user/system，本来就不计）');

  if (models.size) {
    console.log('');
    console.log('—— 计入的调用（按模型 + 四分量）——');
    for (const [k, n] of [...models.entries()].sort((a, b) => b[1] - a[1])) {
      console.log('  ' + String(n).padStart(4) + ' 次  ' + k);
    }
  }
  if (badModels.size) {
    console.log('');
    console.log('—— 被跳过的模型名（这些本该算进去）——');
    for (const [k, n] of [...badModels.entries()].sort((a, b) => b[1] - a[1]).slice(0, 10)) {
      console.log('  ' + String(n).padStart(4) + ' 次  "' + k + '"');
    }
  }

  console.log('');
  console.log('—— 今天写过的文件 ——');
  for (const f of todayFileHits.sort((a, b) => b.n - a.n).slice(0, 12)) {
    console.log('  ' + String(f.n).padStart(4) + ' 条  ' + f.file.replace(CLAUDE_PROJECTS, '…'));
  }

  console.log('');
  console.log('结论怎么看：');
  console.log('  · 「计入统计」的条数 ≈ 接口账单里的请求数 → 数字本该对得上，差在别处');
  console.log('  · 「计入统计」远小于账单请求数 → 记录没写进这个目录，本机统计必然偏少');
  console.log('  · 「模型名不匹配」或「缺稳定 id」有大量记录 → 是解析漏了，能修');
  console.log('');
}

main().catch((err) => {
  console.error('诊断失败：', err && err.message ? err.message : err);
  process.exit(1);
});
