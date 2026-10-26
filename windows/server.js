'use strict';
/*
 * 内置 HTTP 服务 —— Windows 版用它替代 macOS 那边的 pet_daemon.py。
 *
 * 为什么要起一个服务而不是走 IPC：
 *   网页（web/pet.js）是按 fetch('/state') 写的。给它同一个 HTTP 契约，
 *   网页那一侧一行都不用改，两个平台共用同一套动画与交互代码。
 *
 * 只监听 127.0.0.1 + 随机端口，并校验 Host，外部网页读不到任务与花费。
 */

const http = require('http');
const fs = require('fs');
const fsp = fs.promises;
const path = require('path');

const { costSnapshot, usageSnapshot, stateSnapshot } = require('./claude-code');

/**
 * 静态资源根目录。
 *   开发（npm start）→ 项目根目录，直接用仓库里的 pet.html / web / assets
 *   打包后（安装版）→ resources/app-assets，由 electron-builder 的 extraResources 拷进去
 * 不能直接用 __dirname/.. ：打包成 asar 之后那里面只有 js 文件。
 */
function rootDir() {
  try {
    // eslint-disable-next-line global-require
    const { app } = require('electron');
    if (app && app.isPackaged) return path.join(process.resourcesPath, 'app-assets');
  } catch (err) {
    // 纯 Node 环境下跑单测时没有 electron —— 回落到仓库根目录
  }
  return path.resolve(__dirname, '..');
}

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'application/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.svg': 'image/svg+xml',
  '.webp': 'image/webp',
};

// 只允许这几类路径：宠物页 + 它真正需要的静态资源
const PAGES = new Set(['/pet.html']);
const ASSET_DIRS = ['/web/', '/assets/'];
const ASSET_EXT = /\.(js|css|png|jpe?g|svg|webp|json)$/i;

function json(res, body) {
  const buf = Buffer.from(JSON.stringify(body), 'utf8');
  res.writeHead(200, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': buf.length,
    'Cache-Control': 'no-store',
  });
  res.end(buf);
}

function notFound(res) {
  res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' });
  res.end('not found');
}

async function serveStatic(pathname, res) {
  const clean = decodeURIComponent(pathname === '/' ? '/pet.html' : pathname);
  if (clean.includes('..') || clean.includes('\0')) return notFound(res);
  const allowed = PAGES.has(clean)
    || (ASSET_DIRS.some((dir) => clean.startsWith(dir)) && ASSET_EXT.test(clean));
  if (!allowed) return notFound(res);

  const file = path.join(rootDir(), ...clean.split('/').filter(Boolean));
  let buf;
  try {
    buf = await fsp.readFile(file);
  } catch (err) {
    return notFound(res);
  }
  res.writeHead(200, {
    'Content-Type': MIME[path.extname(file).toLowerCase()] || 'application/octet-stream',
    'Content-Length': buf.length,
    'Cache-Control': 'no-store',
  });
  res.end(buf);
}

function localOnly(req) {
  // Host 校验挡住 DNS rebinding：外站即使解析到 127.0.0.1 也读不到
  const host = String(req.headers.host || '');
  return /^(127\.0\.0\.1|localhost|\[::1\]):\d+$/.test(host);
}

async function handle(req, res) {
  if (!localOnly(req)) {
    res.writeHead(403, { 'Content-Type': 'text/plain; charset=utf-8' });
    res.end('local requests only');
    return;
  }
  let pathname;
  try {
    pathname = new URL(req.url, 'http://127.0.0.1').pathname;
  } catch (err) {
    return notFound(res);
  }

  try {
    if (pathname === '/state' || pathname === '/api/state') {
      return json(res, await stateSnapshot());
    }
    if (pathname === '/usage' || pathname === '/api/usage') {
      return json(res, await usageSnapshot());
    }
    if (pathname === '/claude-cost' || pathname === '/api/claude-cost') {
      return json(res, await costSnapshot());
    }
    if (pathname === '/health' || pathname === '/version') {
      return json(res, { ok: true, version: 'win-1.0', source: 'claude' });
    }
    return await serveStatic(pathname, res);
  } catch (err) {
    res.writeHead(500, { 'Content-Type': 'application/json; charset=utf-8' });
    res.end(JSON.stringify({ ok: false, error: String(err && err.message || err) }));
  }
}

/** 起服务，返回 { server, origin }；端口由系统分配，避免和别的程序抢 */
function start() {
  return new Promise((resolve, reject) => {
    const server = http.createServer((req, res) => {
      handle(req, res).catch(() => notFound(res));
    });
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      resolve({ server, port, origin: `http://127.0.0.1:${port}` });
    });
  });
}

module.exports = { start, rootDir };
