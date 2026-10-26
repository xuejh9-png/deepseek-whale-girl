'use strict';
/*
 * 把 macOS 宿主的消息桥（window.webkit.messageHandlers.petHost）接到 Electron IPC。
 *
 * 为什么要做这层 shim：网页（web/pet.js）是按 WKWebView 的接口写的 ——
 *   var host = window.webkit && window.webkit.messageHandlers &&
 *              window.webkit.messageHandlers.petHost;
 * 有了这个同名对象，网页那一侧一行都不用改，两个平台跑同一份前端代码。
 */

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('webkit', {
  messageHandlers: {
    petHost: {
      postMessage: (msg) => ipcRenderer.send('pet-host', msg),
    },
  },
});
