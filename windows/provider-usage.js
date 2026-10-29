'use strict';
// 中转站实扣：只读取本机 Key，异步缓存；不把凭据写进安装包、日志或响应。
const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');
const DIRECT = 'https://api.aishop520.com/v1/usage';
let identity = null, data = null, triedAt = 0, pending = null;

function credentials() {
  const env = process.env;
  if (env.PET_USAGE_KEY) return { key: env.PET_USAGE_KEY.trim(), base: env.PET_USAGE_URL || DIRECT };
  let conf = {};
  try {
    const dir = env.CLAUDE_CONFIG_DIR || path.join(os.homedir(), '.claude');
    conf = JSON.parse(fs.readFileSync(path.join(dir, 'settings.json'), 'utf8')).env || {};
  } catch (err) { /* 未配置 */ }
  const processKey = env.ANTHROPIC_AUTH_TOKEN || env.ANTHROPIC_API_KEY;
  const source = processKey ? env : conf;
  const key = String(source.ANTHROPIC_AUTH_TOKEN || source.ANTHROPIC_API_KEY || '').trim();
  const base = source.ANTHROPIC_BASE_URL || '';
  // 其他供应商的 Key 不应被自动发给这个中转站。
  try {
    const host = new URL(base).hostname;
    if (!['api.aishop520.com', 'localhost', '127.0.0.1', '[::1]'].includes(host)) return null;
  } catch (err) { return null; }
  return key ? { key, base } : null;
}

function endpoint(base) {
  const url = new URL(base);
  const local = ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname);
  if (url.username || url.password || (url.protocol !== 'https:' && !(local && url.protocol === 'http:'))) {
    throw new Error('用量地址需要 HTTPS 或本机转发地址');
  }
  url.pathname = url.pathname.replace(/\/+$/, '').replace(/\/v1(?:\/usage)?$/, '') + '/v1/usage';
  url.search = ''; url.hash = '';
  return url.href;
}

const number = (value) => value !== null && value !== undefined && value !== ''
  && Number.isFinite(Number(value)) ? Number(value) : null;
function normalize(raw, now = new Date()) {
  const total = raw && raw.usage && raw.usage.total;
  const today = raw && raw.usage && raw.usage.today;
  const amount = number(total && total.actual_cost);
  if (amount === null) throw new Error('接口缺少累计实际扣费');
  const bucket = (row) => ({ actualCost: number(row && row.actual_cost),
    requests: number(row && row.requests), tokens: number(row && row.total_tokens),
    listCost: number(row && row.cost) });
  return { amount, currency: raw.unit || 'USD', remaining: number(raw.remaining ?? raw.balance),
    total: bucket(total), today: bucket(today),
    fetchedAt: now.toISOString(), fetchedDay: now.toLocaleDateString(), stale: false };
}

function current() {
  const conf = credentials();
  const id = conf ? crypto.createHash('sha256').update(conf.base + '\0' + conf.key).digest('hex') : null;
  if (id !== identity) {
    identity = id; data = null; triedAt = 0; pending = null;
  }
  return conf;
}

async function refresh() {
  const conf = current();
  if (!conf) return null;
  if (pending) return pending;
  const id = identity;
  triedAt = Date.now();
  const run = async () => {
    try {
      const urls = [...new Set([endpoint(conf.base), DIRECT])];
      for (const url of urls) {
        try {
          const response = await fetch(url, { redirect: 'error', signal: AbortSignal.timeout(8000),
            headers: { Authorization: 'Bearer ' + conf.key, Accept: 'application/json',
              'User-Agent': 'WorkBuddyPet/1.0.1 (' + process.platform + ')', 'Cache-Control': 'no-store' } });
          if (!response.ok) throw new Error('用量接口请求失败');
          const value = normalize(await response.json());
          current(); // 请求期间切换 Key 时，丢弃旧账户返回值。
          if (identity !== id) return null;
          data = value;
          return data;
        } catch (err) { /* 尝试直连，错误不输出凭据或响应正文 */ }
      }
    } catch (err) { /* 配置无效 */ }
    if (identity === id && data) data = { ...data, stale: true };
    return null;
  };
  pending = run();
  try { return await pending; } finally { if (identity === id) pending = null; }
}

function snapshot() {
  if (!current()) return null;
  const ttl = data && !data.stale ? 60000 : 20000;
  if (!pending && Date.now() - triedAt >= ttl) void refresh();
  return data ? { ...data, stale: data.stale || Date.now() - triedAt >= 60000 } : null;
}

function costSnapshot() {
  const value = snapshot();
  if (!value) return null;
  const currency = value.currency;
  return { amount: value.amount, currency, remaining: value.remaining, verified: true,
    estimated: false, source: '中转站 /v1/usage（实际扣费）', updatedAt: value.fetchedAt,
    today: value.today.actualCost === null ? {} : { [currency]: String(value.today.actualCost) },
    requests: value.total.requests, todayRequests: value.today.requests,
    tokens: { total: value.total.tokens, today: value.today.tokens },
    listCost: { total: value.total.listCost, today: value.today.listCost }, stale: value.stale,
    note: '本 API Key 实际扣费；余额为钱包余额或该 Key 的剩余额度'
      + (value.stale ? '；离线，显示最后一次成功读取的值' : '') };
}
module.exports = { credentials, endpoint, normalize, refresh, snapshot, costSnapshot };
