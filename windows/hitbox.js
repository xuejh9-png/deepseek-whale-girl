'use strict';
/*
 * 鼠标穿透的命中判定 —— 单独一个模块，因为它是最容易搞错、又必须能回归的地方。
 *
 * ⚠️ 坐标原点：
 *   Electron/Windows 在**左上**，macOS 的 Cocoa 在**左下**。
 *   desktop/main.swift 里写的是「距底部 5% 起、高 76%」，
 *   换算到这里就是「距顶部 19% 起、高 76%」。
 *   直接照抄 macOS 的数值，会让角色上半身点不到、下半身误穿透。
 *
 * 三个区域都算"她的"：
 *   1. 角色本体（横向 16%~84%，纵向 19%~95%）
 *   2. 顶部「已花费」那一行（17~35px）
 *   3. 底部「今日 Token」那一行（最后 18px）
 * 其余留白一律穿透，不挡你操作别的东西。
 */

const BODY_X0 = 0.16;
const BODY_X1 = 0.84;
const BODY_Y0 = 0.19;   // 从顶部算
const BODY_Y1 = 0.95;
const SUMMARY_TOP = 17; // 与 web/pet.css 的 .pet-summary { top: 17px; height: 18px } 对应
const SUMMARY_H = 18;
const METER_H = 18;

/**
 * @param {{x:number,y:number}} pt 屏幕坐标（DIP）
 * @param {{x:number,y:number,width:number,height:number}} bounds 窗口矩形
 * @returns {boolean} 是否命中「她」
 */
function hitTest(pt, bounds) {
  const rx = pt.x - bounds.x;
  const ry = pt.y - bounds.y;
  if (rx < 0 || ry < 0 || rx > bounds.width || ry > bounds.height) return false;

  const inBody = rx >= bounds.width * BODY_X0
    && rx <= bounds.width * BODY_X1
    && ry >= bounds.height * BODY_Y0
    && ry <= bounds.height * BODY_Y1;

  const inSummary = rx >= 2 && rx <= bounds.width - 2
    && ry >= SUMMARY_TOP && ry <= SUMMARY_TOP + SUMMARY_H;

  const inMeter = rx >= 2 && rx <= bounds.width - 2
    && ry >= bounds.height - METER_H && ry <= bounds.height;

  return inBody || inSummary || inMeter;
}

module.exports = { hitTest, BODY_Y0, BODY_Y1 };
