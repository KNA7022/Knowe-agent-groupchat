#!/usr/bin/env node
/**
 * copy-chromium.mjs — 把 headless_shell 内核拷入 PyInstaller 产物（阶段二 2.3）
 *
 * 背景（对应 00-审计与架构报告 v2 第 7 节，和洲已拍板带 headless_shell）：
 *   - main.ts:629 打包版注入 PLAYWRIGHT_BROWSERS_PATH = resources/backend/ms-playwright
 *   - electron-builder extraResources 把 dist/StudyCrewBackend/ 整体拷为 resources/backend/
 *   - 所以把内核拷到 dist/StudyCrewBackend/ms-playwright/ 即可随包到位
 *
 * 版本匹配（硬约束）：随包内核必须匹配**后端 Python playwright**（浏览器工具是
 *   browser_tools.py 调的，PyInstaller 冻结的就是 conda 环境的 playwright）。
 *   所以期望 revision 从后端 Python 的 playwright browsers.json 读，不是前端
 *   node_modules 的 playwright-core（两者可能不同版本！实测前端 1.62.0→rev1234，
 *   后端 1.61.0→rev1228，用错会导致 playwright 拒绝启动）。
 *
 * Python 探测：STUDYCREW_DEV_PYTHON → KNOWE_DEV_PYTHON → py -3.12 → python
 *   （与 preflight.mjs / backend-build.mjs 同序，且**强制 3.12 闸门**：
 *    非 3.12 的解释器会被 probe 拒绝并打印原因，避免用错解释器读到错误 revision）。
 *
 * 幂等：目标已存在且完整时跳过。
 */

import { existsSync, statSync, readdirSync, mkdirSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { parsePythonCommand } from './python-command.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const DIST_BACKEND = join(ROOT, 'dist', 'StudyCrewBackend');

/** 开发机 playwright 浏览器缓存根（playwright 库原生定位路径）。 */
//   [macOS R4] 按平台：win32 = %USERPROFILE%\AppData\Local\ms-playwright，darwin = ~/Library/Caches/ms-playwright。
const MS_PLAYWRIGHT_ROOT = process.platform === 'win32'
  ? join(homedir(), 'AppData', 'Local', 'ms-playwright')
  : join(homedir(), 'Library', 'Caches', 'ms-playwright');

function backendExpectedRevision() {
  // 用后端 Python 读 playwright 的期望浏览器 revision（单一事实源）。
  // 候选顺序与 preflight.mjs / backend-build.mjs 保持一致：显式覆盖 → py -3.12 → python。
  // 曾用裸 `python`/`python3`，在装了多个解释器的机器上会读到**另一个** playwright 的
  // revision，从而拷入错误版本的内核（playwright 会拒绝启动）。
  const candidates = [
    process.env.STUDYCREW_DEV_PYTHON,
    process.env.KNOWE_DEV_PYTHON,
    process.platform === 'win32' ? 'py -3.12' : 'python3.12',
    process.platform === 'win32' ? 'python' : 'python3',
  ].filter((value, index, all) => value && all.indexOf(value) === index);
  const probe = `
import json, pathlib, sys
if sys.version_info[:2] != (3, 12):
    raise SystemExit(f"这个解释器是 {sys.version_info.major}.{sys.version_info.minor}，不是 3.12")
try:
    import playwright
except ImportError:
    raise SystemExit("playwright 未安装")
p = pathlib.Path(playwright.__file__).parent / "driver" / "package" / "browsers.json"
d = json.loads(p.read_text(encoding="utf-8"))
for b in d["browsers"]:
    if b["name"] == "chromium-headless-shell":
        print(b["revision"])
        break
else:
    raise SystemExit("browsers.json 里找不到 chromium-headless-shell")
`;
  const problems = [];
  for (const candidate of candidates) {
    const { command, args } = parsePythonCommand(candidate);
    const r = spawnSync(command, [...args, '-c', probe], { encoding: 'utf8' });
    if (r.error?.code === 'ENOENT') continue;
    if (r.status === 0) {
      const rev = r.stdout.trim();
      if (/^\d+$/.test(rev)) {
        console.log(`[copy-chromium] 期望 revision 取自：${candidate} → ${rev}`);
        return rev;
      }
      continue;
    }
    // 版本闸门：probe 现在会拒绝非 3.12 的解释器。必须把原因说出来，否则
    // "静默回落到另一个解释器"正是我们要消除的那个 bug。
    const reason = (r.stderr || '').trim().split('\n').filter(Boolean).pop() ?? `退出码 ${r.status}`;
    problems.push(`${candidate}: ${reason}`);
  }
  for (const problem of problems) console.error(`[copy-chromium] ${problem}`);
  throw new Error(
    '无法从后端 Python(3.12) 读取 playwright 期望 revision；' +
      '确认该解释器装了 playwright（py -3.12 -m pip install playwright），或用 STUDYCREW_DEV_PYTHON 指定。',
  );
}

function dirSize(dir) {
  let total = 0;
  for (const entry of readDirRec(dir)) total += entry.size;
  return total;
}

function readDirRec(dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    const st = statSync(p);
    if (st.isDirectory()) out.push(...readDirRec(p));
    else out.push({ path: p, size: st.size });
  }
  return out;
}

async function main() {
  if (!existsSync(DIST_BACKEND)) {
    console.error(`[copy-chromium] 找不到 PyInstaller 产物：${DIST_BACKEND}`);
    console.error('先跑后端打包（PyInstaller），再跑本脚本。');
    process.exit(1);
  }

  const rev = backendExpectedRevision();
  const src = join(MS_PLAYWRIGHT_ROOT, `chromium_headless_shell-${rev}`);
  if (!existsSync(src)) {
    console.error(`[copy-chromium] 开发机缓存缺 chromium_headless_shell-${rev}：${src}`);
    console.error(`运行（用打包后端的同一个解释器）：py -3.12 -m playwright install chromium-headless-shell`);
    console.error('或用 STUDYCREW_DEV_PYTHON 指定该解释器后重跑本脚本。');
    process.exit(1);
  }

  const dst = join(DIST_BACKEND, 'ms-playwright', `chromium_headless_shell-${rev}`);

  // 幂等：目标存在且字节数一致 → 跳过
  if (existsSync(dst) && dirSize(dst) === dirSize(src)) {
    console.log(`[copy-chromium] 幂等跳过：${dst}（rev ${rev}，${(dirSize(src) / 1024 / 1024).toFixed(0)}MB）`);
    return;
  }

  console.log(`[copy-chromium] 拷贝 headless_shell rev ${rev}（${(dirSize(src) / 1024 / 1024).toFixed(0)}MB）`);
  console.log(`  源：${src}`);
  console.log(`  目标：${dst}`);
  // 大目录（271MB、数万文件）拷贝，按平台选命令：
  //   - win32：robocopy（Windows 原生，业界标准）/E 递归含空目录、/COPY:DAT 数据+属性+时间戳、
  //     /R:1 失败重试1次、/W:1 等待1秒；robocopy 退出码 0-7 都是成功（8+ 才是失败）
  //   - darwin：cp -R（POSIX 原生，递归拷贝）；先建父目录（cp 不自动建）
  if (process.platform === 'win32') {
    const r = spawnSync('robocopy', [src, dst, '/E', '/COPY:DAT', '/R:1', '/W:1', '/NFL', '/NDL', '/NJH', '/NJS'], {
      encoding: 'utf8',
    });
    if (r.error || (r.status ?? 0) >= 8) {
      console.error('[copy-chromium] robocopy 失败：', r.error?.message ?? `exit ${r.status}`);
      process.exit(1);
    }
  } else {
    mkdirSync(dirname(dst), { recursive: true });
    const r = spawnSync('cp', ['-R', src, dst], { encoding: 'utf8' });
    if (r.error || (r.status ?? 0) !== 0) {
      console.error('[copy-chromium] cp -R 失败：', r.error?.message ?? `exit ${r.status}`);
      process.exit(1);
    }
  }
  console.log(`[copy-chromium] 完成：${(dirSize(dst) / 1024 / 1024).toFixed(0)}MB 已就位`);
}

main().catch((err) => {
  console.error('[copy-chromium] 失败：', err.message);
  process.exit(1);
});
