'use strict';
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'pet-provider-'));
const originalEnv = { ...process.env };
const originalFetch = global.fetch;
delete process.env.ANTHROPIC_AUTH_TOKEN;
delete process.env.ANTHROPIC_API_KEY;
delete process.env.PET_USAGE_KEY;
delete process.env.PET_USAGE_URL;
process.env.CLAUDE_CONFIG_DIR = tmp;
const p = require('./provider-usage');
const sample = { remaining: 80, usage: {
  today: { actual_cost: 1.25, requests: 3, total_tokens: 100, cost: 2 },
  total: { actual_cost: 20, requests: 50, total_tokens: 2000, cost: 30 } } };
let passed = 0;
function check(name, fn) { fn(); passed++; console.log('  ✓ ' + name); }
function key(value) { process.env.PET_USAGE_KEY = value; }
function response(raw = sample) { return { ok: true, json: async () => raw }; }
(async () => {
  try {
    check('没有 Key 不发请求', () => {
      global.fetch = () => { throw new Error('不应调用'); };
      assert.strictEqual(p.snapshot(), null);
    });
    for (const suffix of ['', '/', '/v1', '/v1/', '/v1/usage']) {
      check('地址归一化 ' + suffix, () => assert.strictEqual(
        p.endpoint('https://api.aishop520.com' + suffix), 'https://api.aishop520.com/v1/usage'));
    }
    check('外网明文地址禁止', () => assert.throws(() => p.endpoint('http://example.com')));
    check('只允许本机 HTTP 转发', () => assert.strictEqual(
      p.endpoint('http://127.0.0.1:8765/v1'), 'http://127.0.0.1:8765/v1/usage'));
    check('不把缺失金额伪装成零', () => assert.throws(() => p.normalize({ usage: {} })));
    check('实际扣费不乘倍率', () => assert.strictEqual(p.normalize(sample).amount, 20));
    check('零余额是有效数值', () => assert.strictEqual(p.normalize({ ...sample, remaining: 0 }).remaining, 0));
    check('没有余额则保持未知', () => assert.strictEqual(p.normalize({ usage: sample.usage }).remaining, null));
    fs.writeFileSync(path.join(tmp, 'settings.json'), JSON.stringify({ env: {
      ANTHROPIC_AUTH_TOKEN: 'other-provider-key', ANTHROPIC_BASE_URL: 'https://api.anthropic.com' } }));
    check('其他供应商 Key 不发给中转站', () => assert.strictEqual(p.credentials(), null));
    key('test-key-a');
    process.env.PET_USAGE_URL = 'https://api.aishop520.com/v1/usage';
    const calls = [];
    global.fetch = async (url, options) => { calls.push({ url, options }); return response(); };
    await p.refresh();
    check('接口 URL 正确', () => assert.strictEqual(calls[0].url, 'https://api.aishop520.com/v1/usage'));
    check('请求带专用 UA', () => assert(calls[0].options.headers['User-Agent'].startsWith('WorkBuddyPet/')));
    check('禁止自动跳转携带凭据', () => assert.strictEqual(calls[0].options.redirect, 'error'));
    check('优先显示实扣并带余额', () => {
      const snap = p.costSnapshot();
      assert.strictEqual(snap.amount, 20); assert.strictEqual(snap.remaining, 80);
      assert.strictEqual(snap.estimated, false); assert.strictEqual(snap.today.USD, '1.25');
      assert(!JSON.stringify(snap).includes('test-key-a'));
    });
    check('缓存避免重复请求', () => { p.snapshot(); p.snapshot(); assert.strictEqual(calls.length, 1); });
    global.fetch = async () => { throw new Error('offline'); };
    await p.refresh();
    check('断网旧值明确标记', () => { assert(p.costSnapshot().stale); assert.strictEqual(p.costSnapshot().amount, 20); });
    key('test-key-b');
    check('换 Key 立即清除旧余额', () => assert.strictEqual(p.snapshot(), null));
    await p.refresh();
    key('test-key-c');
    let release;
    global.fetch = () => new Promise((resolve) => { release = resolve; });
    const pending = p.refresh();
    key('test-key-d');
    p.credentials();
    release(response());
    await pending;
    global.fetch = async () => response({ ...sample, remaining: 40 });
    check('切 Key 时丢弃在途旧请求', () => assert.strictEqual(p.snapshot(), null));
    await p.refresh();
    check('新 Key 只显示新余额', () => assert.strictEqual(p.costSnapshot().remaining, 40));
    const home = os.homedir;
    os.homedir = () => tmp;
    let sources;
    try { sources = require('./sources'); } finally { os.homedir = home; }
    const usage = await sources.usageSnapshot();
    check('中转站 Token 替换本地值，不叠加', () => {
      assert.strictEqual(usage.today.bySource.claude.totalTokens, 100);
      assert.strictEqual(usage.today.bySource.claude.basis, 'provider');
      assert.strictEqual(usage.today.totalTokens, 100);
    });
    const { server, origin } = await require('./server').start();
    try {
      const cost = await (await originalFetch(origin + '/claude-cost')).json();
      check('HTTP 接口优先实际扣费', () => {
        assert.strictEqual(cost.amount, 20); assert.strictEqual(cost.remaining, 40);
        assert.strictEqual(cost.estimated, false);
      });
    } finally {
      await new Promise((resolve) => { server.close(resolve); server.closeAllConnections(); });
    }
    console.log(`\n  ${passed} 项通过`);
  } finally {
    global.fetch = originalFetch;
    for (const name of Object.keys(process.env)) if (!(name in originalEnv)) delete process.env[name];
    Object.assign(process.env, originalEnv);
    fs.rmSync(tmp, { recursive: true, force: true });
  }
})().catch((err) => { console.error(err); process.exitCode = 1; });
