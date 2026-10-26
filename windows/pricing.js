'use strict';
/*
 * Claude 价目表 —— 与 macOS 版 claude_cost.py 同一份数据、同一套匹配规则。
 *
 * 两份实现（Python / JS）必须给出相同金额，否则桌宠在 Windows 上算出来的
 * 数字会和面板对不上。改这里的价目，记得同步 claude_cost.py。
 *
 * 顺序是「输入、输出、缓存读、缓存写」—— 和 Anthropic 官网表格的列序不同，
 * 照抄会把缓存读和写算反。
 */

const fs = require('fs');
const os = require('os');
const path = require('path');

const CC_PRICING = path.join(os.homedir(), '.cc-switch', 'model-pricing.json');
const PRICING_ENV = 'PET_CLAUDE_PRICING';

const FALLBACK_PRICES = {
  'claude-fable-5-1': [10.0, 50.0, 0.25, 12.5],
  'claude-fable-5': [10.0, 50.0, 1.0, 12.5],
  'claude-opus-5-5': [4.0, 20.0, 0.2, 5.0],
  'claude-opus-5': [5.0, 25.0, 0.5, 6.25],
  'claude-sonnet-5-5': [2.0, 10.0, 0.1, 2.5],
  'claude-sonnet-5': [2.0, 10.0, 0.2, 2.5],
  'claude-haiku-5-5': [0.1, 0.5, 0.01, 0.125],
  'claude-opus-4-8': [5.0, 25.0, 0.5, 6.25],
  'claude-opus-4-7': [5.0, 25.0, 0.5, 6.25],
  'claude-opus-4-6': [5.0, 25.0, 0.5, 6.25],
  'claude-opus-4-5': [5.0, 25.0, 0.5, 6.25],
  'claude-opus-4-1': [15.0, 75.0, 1.5, 18.75],
  'claude-opus-4': [15.0, 75.0, 1.5, 18.75],
  'claude-opus-3': [15.0, 75.0, 1.5, 18.75],
  'claude-sonnet-4-6': [3.0, 15.0, 0.3, 3.75],
  'claude-sonnet-4-5': [3.0, 15.0, 0.3, 3.75],
  'claude-sonnet-4': [3.0, 15.0, 0.3, 3.75],
  'claude-sonnet-3-7': [3.0, 15.0, 0.3, 3.75],
  'claude-sonnet-3-5': [3.0, 15.0, 0.3, 3.75],
  'claude-haiku-4-5': [1.0, 5.0, 0.1, 1.25],
  'claude-haiku-3-5': [0.8, 4.0, 0.08, 1.0],
  'claude-haiku-3': [0.25, 1.25, 0.03, 0.3],
};

const FAMILIES = '(opus|sonnet|haiku|fable)';

/** 型号名 → { family, major, minor }；认不出返回 null。 */
function parseModel(name) {
  const low = String(name || '').trim().toLowerCase();
  let m = low.match(new RegExp(`claude-${FAMILIES}-(\\d+)(?:[-.](\\d+))?`));
  if (m) return { family: m[1], major: Number(m[2]), minor: Number(m[3] || 0) };
  // 旧写法：claude-3-5-sonnet-20241022
  m = low.match(new RegExp(`claude-(\\d+)(?:[-.](\\d+))?-${FAMILIES}`));
  if (m) return { family: m[3], major: Number(m[1]), minor: Number(m[2] || 0) };
  return null;
}

function pricingPath() {
  const override = process.env[PRICING_ENV];
  return override ? override.replace(/^~(?=$|[\\/])/, os.homedir()) : CC_PRICING;
}

/** 价目表：外部表优先（代表用户实际口径），内置表只补缺口。 */
function priceTable() {
  const table = Object.create(null);
  try {
    const raw = JSON.parse(fs.readFileSync(pricingPath(), 'utf8'));
    for (const row of raw.models || []) {
      const id = String(row.modelId || '').trim().toLowerCase();
      if (!id.startsWith('claude-')) continue;
      const nums = ['inputCostPerMillion', 'outputCostPerMillion',
        'cacheReadCostPerMillion', 'cacheCreationCostPerMillion']
        .map((k) => Number(row[k] || 0));
      if (nums.some((n) => !Number.isFinite(n))) continue;
      table[id] = nums;
    }
  } catch (err) {
    // 没有 CC Switch / 文件坏了 → 用内置表，不报错
  }
  for (const [id, price] of Object.entries(FALLBACK_PRICES)) {
    if (!(id in table)) table[id] = price.slice();
  }
  return table;
}

/** 型号 → 单价四元组；找不到返回 null。 */
function priceOf(model, table) {
  const key = String(model || '').trim().toLowerCase();
  if (!key) return null;
  if (key in table) return table[key];
  for (const [id, price] of Object.entries(table)) {
    if (key.startsWith(id)) return price;
  }
  const parsed = parseModel(key);
  if (!parsed) return null;
  const { family, major, minor } = parsed;
  for (const cand of [`claude-${family}-${major}-${minor}`, `claude-${family}-${major}`]) {
    if (cand in table) return table[cand];
  }
  // 同家族同主版本：同代价格通常一致
  const sameMajor = Object.keys(table)
    .filter((id) => {
      const p = parseModel(id);
      return p && p.family === family && p.major === major;
    })
    .sort();
  if (sameMajor.length) return table[sameMajor[0]];
  // 同家族最接近的主版本
  let best = null;
  for (const id of Object.keys(table)) {
    const p = parseModel(id);
    if (!p || p.family !== family) continue;
    const dist = Math.abs(p.major - major);
    if (!best || dist < best.dist || (dist === best.dist && p.major > best.major)) {
      best = { dist, major: p.major, price: table[id] };
    }
  }
  return best ? best.price : null;
}

module.exports = { FALLBACK_PRICES, CC_PRICING, PRICING_ENV, parseModel, pricingPath, priceTable, priceOf };
