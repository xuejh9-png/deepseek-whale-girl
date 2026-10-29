'use strict';
// Electron 实际渲染回归。所有接口使用合成数据，不读取真实会话或费用。
const { app, BrowserWindow } = require('electron');
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'pet-ui-'));
app.setPath('userData', path.join(tmp, 'userdata'));
app.disableHardwareAcceleration();
app.on('window-all-closed', () => {});
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
function window() {
  return new BrowserWindow({ width: 160, height: 200, useContentSize: true,
    show: false, webPreferences: { backgroundThrottling: false, offscreen: true } });
}

app.whenReady().then(async () => {
  let server;
  try {
    for (const file of ['pet-runtime.test.html', 'pet-bridge.test.html',
      'pet-wake.test.html', 'pet-metrics.test.html']) {
      const win = window();
      await win.loadFile(path.resolve(__dirname, '..', file));
      const deadline = Date.now() + 35000;
      let title = '';
      do {
        await delay(250);
        title = win.getTitle();
      } while (!title.startsWith('RESULT|') && Date.now() < deadline);
      console.log(file + ': ' + title);
      assert(title.startsWith('RESULT|') && title.includes('FAIL=0'), title);
      win.destroy();
    }

    const sources = require('./sources');
    require('./provider-usage').snapshot = () => null;
    require('./provider-usage').costSnapshot = () => null;
    let state = 'working';
    sources.stateSnapshot = async () => ({ state, source: 'claude', activeSessions: 1 });
    sources.usageSnapshot = async () => ({ today: { totalTokens: 123456,
      bySource: {}, complete: true, dayLocal: '2026-10-29', note: '合成测试' } });
    require('./claude-code').costSnapshot = async () => ({ amount: 12.34,
      currency: 'USD', verified: true, source: '合成测试' });
    const started = await require('./server').start();
    server = started.server;
    const win = window();
    await win.loadURL(started.origin + '/pet.html');
    await delay(500);
    assert.strictEqual(await win.webContents.executeJavaScript("document.querySelector('#pet').dataset.state"), 'working');
    state = 'success';
    await delay(2300);
    assert.strictEqual(await win.webContents.executeJavaScript("document.querySelector('#pet').dataset.state"), 'success');
    console.log('真实 HTTP /state → 网页 → 动画状态：通过');
    await win.webContents.insertCSS('.pet-body {animation:none !important}');
    let base;
    for (const [w, h] of [[160, 200], [220, 260], [200, 240], [160, 200]]) {
      win.setContentSize(w, h);
      await delay(100);
      const rect = await win.webContents.executeJavaScript(`(() => {
        const b = document.querySelector('.pet-body').getBoundingClientRect();
        const c = document.querySelector('.pet-summary').getBoundingClientRect();
        const m = document.querySelector('.pet-meter').getBoundingClientRect();
        return [c.x-b.x, c.y-b.y, m.x-b.x, m.y-b.y, b.width, b.height];
      })()`);
      if (!base) base = rect;
      assert.deepStrictEqual(rect, base, '窗口变化后卡片必须跟随角色');
    }
    console.log('卡片/角色相对位置，4 组窗口尺寸：通过');
    if (process.env.PET_TEST_SCREENSHOT) {
      const shot = await win.webContents.capturePage();
      fs.writeFileSync(process.env.PET_TEST_SCREENSHOT, shot.toPNG());
    }
    win.destroy();
  } catch (err) {
    console.error(err);
    process.exitCode = 1;
  } finally {
    if (server) await new Promise((resolve) => {
      server.close(resolve); server.closeAllConnections();
    });
    app.exit(process.exitCode || 0);
  }
});
