#!/usr/bin/env node
/**
 * check-backend-artifact.mjs — 校验 PyInstaller 产物是否"完整可交付"
 *
 * 为什么需要：`backend:build` 用 `--noconfirm` 整体替换 COLLECT 目录
 * （`dist/StudyCrewBackend/`），这会把先前由 `copy:chromium` 拷进去的
 * Playwright 浏览器内核（约 267 MiB）**一并删掉**。于是：
 *   - `npm run dist:win`（完整链）没问题，它后面会重跑 copy:chromium；
 *   - 但 `npm run dist:win:app`（TECH.md 记录的第 4 步）会跳过第 3 步，
 *     静默产出一个缺少浏览器内核的安装包 —— 与"版本资源静默失败"同一类问题。
 *
 * 因此这里把"产物完整性"变成可执行的检查：缺内核时明确失败并给出补救命令。
 *
 * 用法：node scripts/check-backend-artifact.mjs [--allow-missing-chromium]
 *   默认要求 exe + _internal + ms-playwright 齐全；`--allow-missing-chromium`
 *   用于"刚跑完 backend:build、还没 copy:chromium"的合法中间态。
 */

import { existsSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const DIST = join(ROOT, 'dist', 'StudyCrewBackend');
const EXE = join(DIST, process.platform === 'win32' ? 'StudyCrewBackend.exe' : 'StudyCrewBackend');
const INTERNAL = join(DIST, '_internal');
const PW = join(DIST, 'ms-playwright');
const allowMissingChromium = process.argv.includes('--allow-missing-chromium');

const problems = [];

if (!existsSync(EXE)) {
  problems.push(`缺少后端可执行文件：${EXE}（先跑 npm run backend:build）`);
}
if (!existsSync(INTERNAL)) {
  problems.push(`缺少 _internal/：${INTERNAL}（PyInstaller 产物不完整，请重跑 backend:build）`);
}

const hasChromium = existsSync(PW)
  ? readdirSync(PW).some((name) => name.startsWith('chromium_headless_shell-'))
  : false;
if (!hasChromium) {
  const message =
    `缺少 Playwright 浏览器内核：${PW}\\chromium_headless_shell-*/` +
    '（backend:build 会用 --noconfirm 整体替换 COLLECT 目录，把内核删掉）';
  if (allowMissingChromium) {
    console.warn(`[check-backend-artifact] ⚠ ${message}`);
    console.warn('[check-backend-artifact]   中间态可接受；打包前请跑 npm run copy:chromium');
  } else {
    problems.push(`${message} → 跑 npm run copy:chromium 补回，或用 npm run dist:win 走完整链`);
  }
}

if (problems.length) {
  for (const problem of problems) console.error(`❌ check-backend-artifact: ${problem}`);
  process.exit(1);
}

const size = (dir) => {
  let total = 0;
  const stack = [dir];
  while (stack.length) {
    const current = stack.pop();
    for (const entry of readdirSync(current, { withFileTypes: true })) {
      const full = join(current, entry.name);
      if (entry.isDirectory()) stack.push(full);
      else total += statSync(full).size;
    }
  }
  return total;
};

console.log(
  `✅ check-backend-artifact: 产物完整（exe + _internal + chromium 内核，共 ${(
    size(DIST) /
    1024 /
    1024
  ).toFixed(1)} MiB）`,
);
