'use strict';
/*
 * Windows 版数据层回归 —— 不需要 Electron，纯 Node 就能跑。
 *
 *   node windows/test-claude-code.js
 *
 * 用合成日志 + 受控时间戳测，不依赖本机此刻是否在跑 Claude Code。
 * 这些断言钉住的是「和 macOS 版同口径」这件事：
 * 状态窗口、去重规则、四条分量计价，改任何一条都会在这里红。
 */

const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

let passed = 0;
function ok(name, cond, got) {
  if (cond) {
    passed += 1;
    console.log(`  ✓ ${name}`);
  } else {
    console.log(`  ✗ ${name}  ← 实得 ${JSON.stringify(got)}`);
    process.exitCode = 1;
  }
}

/** 每个用例换一个目录，重新加载模块（目录是模块级常量） */
function load(dir) {
  process.env.PET_CLAUDE_DIR = dir;
  delete require.cache[require.resolve('./claude-code')];
  delete require.cache[require.resolve('./pricing')];
  return require('./claude-code');
}

function tmpdir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'pet-cc-'));
}

function assistant({ ago, model = 'claude-sonnet-4-5-20250929', id = 'msg_1',
  input = 1000, output = 500, cacheRead = 0, cacheCreation = 0, tool = false }) {
  const ts = new Date(Date.now() - ago * 1000).toISOString();
  return JSON.stringify({
    type: 'assistant',
    timestamp: ts,
    cwd: '/demo/project',
    sessionId: 'sess-demo',
    message: {
      id,
      model,
      usage: {
        input_tokens: input,
        output_tokens: output,
        cache_read_input_tokens: cacheRead,
        cache_creation_input_tokens: cacheCreation,
      },
      content: tool
        ? [{ type: 'tool_use', name: 'Read', input: {} }]
        : [{ type: 'text', text: 'ok' }],
    },
  });
}

function user(ago) {
  return JSON.stringify({
    type: 'user',
    timestamp: new Date(Date.now() - ago * 1000).toISOString(),
    cwd: '/demo/project',
    sessionId: 'sess-demo',
    message: { role: 'user', content: '帮我看看这段代码' },
  });
}

function write(dir, lines, name = 'sess-demo.jsonl') {
  const sub = path.join(dir, '-demo-project');
  fs.mkdirSync(sub, { recursive: true });
  fs.writeFileSync(path.join(sub, name), lines.join('\n') + '\n', 'utf8');
}

(async () => {
  console.log('状态推断：');

  // ---- 空目录 ----
  {
    const dir = tmpdir();
    const cc = load(dir);
    const s = await cc.stateSnapshot();
    ok('没有任何日志 → offline', s.state === 'offline', s.state);
  }

  // ---- 正在调工具 ----
  {
    const dir = tmpdir();
    write(dir, [assistant({ ago: 5, tool: true })]);
    const cc = load(dir);
    const s = await cc.stateSnapshot();
    ok('5 秒前在调工具 → working', s.state === 'working', s.state);
    ok('带上 cwd', s.cwd === '/demo/project', s.cwd);
  }

  // ---- 刚说完话 ----
  {
    const dir = tmpdir();
    write(dir, [assistant({ ago: 3 })]);
    const cc = load(dir);
    const s = await cc.stateSnapshot();
    ok('3 秒前回复完 → success', s.state === 'success', s.state);
  }

  // ---- 助手回完一段，球在你这边 ----
  {
    const dir = tmpdir();
    write(dir, [assistant({ ago: 40 })]);
    const cc = load(dir);
    const s = await cc.stateSnapshot();
    ok('40 秒前回复完 → waiting', s.state === 'waiting', s.state);
  }

  // ---- 安静够久 ----
  {
    const dir = tmpdir();
    write(dir, [assistant({ ago: 61 })]);
    const cc = load(dir);
    const s = await cc.stateSnapshot();
    ok('61 秒没动静 → sleeping', s.state === 'sleeping', s.state);
  }

  // ---- 用户刚发言 ----
  {
    const dir = tmpdir();
    write(dir, [assistant({ ago: 30 }), user(3)]);
    const cc = load(dir);
    const s = await cc.stateSnapshot();
    ok('用户 3 秒前发言 → thinking', s.state === 'thinking', s.state);
  }

  // ---- 只有元数据、没有业务记录的临时会话不能选中 ----
  {
    const dir = tmpdir();
    write(dir, [assistant({ ago: 600 })], 'real.jsonl');
    const meta = JSON.stringify({ type: 'cost-state', sessionId: 'sess-meta' });
    write(dir, [meta], 'meta.jsonl');
    // 让 meta.jsonl 成为 mtime 最新的文件
    const p = path.join(dir, '-demo-project', 'meta.jsonl');
    const future = new Date(Date.now() + 5000);
    fs.utimesSync(p, future, future);
    const cc = load(dir);
    const s = await cc.stateSnapshot();
    ok('mtime 很新但没有业务记录的文件被跳过', s.state === 'sleeping', s.state);
  }

  console.log('花费与去重：');

  // ---- 四条分量分别计价 ----
  {
    const dir = tmpdir();
    write(dir, [assistant({
      ago: 10, input: 1000000, output: 1000000, cacheRead: 1000000, cacheCreation: 1000000,
    })]);
    const cc = load(dir);
    const c = await cc.costSnapshot();
    // sonnet-4-5 = 3 / 15 / 0.3 / 3.75，四条各 1M
    ok('四条分量分别计价 = 22.05', Math.abs(c.amount - 22.05) < 1e-6, c.amount);
    ok('调用次数 1', c.requests === 1, c.requests);
    ok('token 合计 4M', c.tokens.total === 4000000, c.tokens.total);
  }

  // ---- 同一 message id 的流式累加取最大值，不重复计 ----
  {
    const dir = tmpdir();
    write(dir, [
      assistant({ ago: 20, id: 'msg_stream', input: 1000, output: 200 }),
      assistant({ ago: 18, id: 'msg_stream', input: 1000, output: 900 }),
    ]);
    const cc = load(dir);
    const c = await cc.costSnapshot();
    ok('同一 message 只算一次', c.requests === 1, c.requests);
    ok('取各分量最大值（output 900）', c.tokens.total === 1000 + 900, c.tokens.total);
  }

  // ---- 两个不同请求必须各算一次 ----
  {
    const dir = tmpdir();
    write(dir, [
      assistant({ ago: 20, id: 'msg_a' }),
      assistant({ ago: 10, id: 'msg_b' }),
    ]);
    const cc = load(dir);
    const c = await cc.costSnapshot();
    ok('两个不同请求算两次', c.requests === 2, c.requests);
  }

  // ---- 非 Claude 模型不计 ----
  {
    const dir = tmpdir();
    write(dir, [assistant({ ago: 5, model: 'gpt-5' })]);
    const cc = load(dir);
    const c = await cc.costSnapshot();
    ok('非 Claude 模型不计入', c.requests === 0 && c.amount === 0, c);
  }

  // ---- 带日期 / 旧写法都能定价 ----
  {
    const dir = tmpdir();
    write(dir, [
      assistant({ ago: 30, id: 'm1', model: 'claude-sonnet-4-5-20250929' }),
      assistant({ ago: 20, id: 'm2', model: 'claude-3-5-sonnet-20241022' }),
      assistant({ ago: 10, id: 'm3', model: 'claude-opus-4-1' }),
    ]);
    const cc = load(dir);
    const c = await cc.costSnapshot();
    ok('三种写法都能计价', c.requests === 3, c.requests);
    ok('金额大于 0', c.amount > 0, c.amount);
  }

  console.log('今日口径：');

  {
    const dir = tmpdir();
    write(dir, [assistant({ ago: 5 })]);
    const cc = load(dir);
    const u = await cc.usageSnapshot();
    ok('今日 token 有值', u.today.totalTokens > 0, u.today.totalTokens);
    ok('bySource.claude 与总额一致',
      u.today.bySource.claude.totalTokens === u.today.totalTokens, u.today.bySource);
    ok('complete=true（她的世界里只有 Claude 一个来源）', u.today.complete === true, u.today.complete);
  }

  console.log('鼠标穿透命中区：');

  {
    const { hitTest } = require('./hitbox');
    const box = { x: 1000, y: 500, width: 160, height: 200 };
    const at = (rx, ry) => hitTest({ x: 1000 + rx, y: 500 + ry }, box);
    ok('角色胸口（横 50%、竖 50%）可点', at(80, 100) === true, at(80, 100));
    ok('角色下半身（竖 85%）可点', at(80, 170) === true, at(80, 170));
    ok('左上角留白穿透', at(5, 5) === false, at(5, 5));
    // 注意别选到底部那行按钮上 —— 那一行本来就是可点的
    ok('右侧留白穿透（角色外、非文字行）', at(155, 150) === false, at(155, 150));
    ok('顶部「已花费」那一行可点', at(80, 25) === true, at(80, 25));
    ok('底部「今日 Token」那一行可点', at(80, 190) === true, at(80, 190));
    // 关键回归：macOS 的 5%~81% 照抄过来会让竖 90% 处穿透
    ok('Y 轴按顶部原点换算（竖 90% 仍是角色）', at(80, 180) === true, at(80, 180));
  }

  console.log(`\n  ${passed} 项通过${process.exitCode ? '，有失败' : ''}`);
})();
