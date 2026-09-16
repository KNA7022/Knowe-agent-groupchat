#!/usr/bin/env node
/**
 * backend-build.mjs — 用 PyInstaller 打包 Python 后端（阶段 0 新增）
 *
 * 为什么需要这个脚本：
 *   此前 TECH.md 只给了一条手工命令，且 build/ 与 dist/ 都被 .gitignore 忽略，
 *   导致"每个新检出都无法产出安装包"——electron-builder.yml 的
 *   extraResources.from: dist/StudyCrewBackend 指向一个从不存在的目录。
 *   本脚本把这条链路变成一条可重复执行的命令（npm run backend:build）。
 *
 * 产物：
 *   dist/StudyCrewBackend/StudyCrewBackend.exe   ← electron-builder 从这里取
 *
 * 可选依赖说明：web_tools.py / browser_tools.py / file_ops.py 对
 *   ddgs、playwright、yaml 都是 **延迟导入**，缺失时降级为可读提示。
 *   因此它们不是构建必需，但装了才会被打进产物。脚本会报告哪些存在。
 *
 * 幂等：产物已存在且比 backend/ 源码新时跳过，可用 --force 强制重建。
 */

import { existsSync, readdirSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const BACKEND = join(ROOT, 'backend');
const SPEC = join(BACKEND, 'StudyCrewBackend.spec');
const DIST = join(ROOT, 'dist', 'StudyCrewBackend');
const EXE = join(DIST, process.platform === 'win32' ? 'StudyCrewBackend.exe' : 'StudyCrewBackend');
const force = process.argv.includes('--force');

/** Python 候选，与 preflight.mjs 同顺序（显式覆盖优先）。 */
const PYTHON_CANDIDATES = [
  process.env.STUDYCREW_DEV_PYTHON,
  process.env.KNOWE_DEV_PYTHON,
  process.platform === 'win32' ? 'py -3.12' : 'python3.12',
  process.platform === 'win32' ? 'python' : 'python3',
].filter((value, index, all) => value && all.indexOf(value) === index);

function splitCommand(spec) {
  const parts = spec.trim().split(/\s+/);
  return { command: parts[0], args: parts.slice(1) };
}

/** 选一个能跑起来、版本为 3.12、且装了 PyInstaller 的解释器。 */
function selectPython() {
  const problems = [];
  for (const candidate of PYTHON_CANDIDATES) {
    const { command, args } = splitCommand(candidate);
    const probe = spawnSync(
      command,
      [...args, '-c', 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")'],
      { cwd: ROOT, encoding: 'utf8' },
    );
    if (probe.error?.code === 'ENOENT') continue;
    if (probe.status !== 0) {
      problems.push(`${candidate}: 无法执行`);
      continue;
    }
    const version = probe.stdout.trim();
    if (version !== '3.12') {
      problems.push(`${candidate}: 版本 ${version}，需要 3.12`);
      continue;
    }
    const mods = spawnSync(
      command,
      [...args, '-c', 'import importlib.util as u;print(" ".join(m for m in ("PyInstaller","playwright","ddgs","yaml") if u.find_spec(m)))'],
      { cwd: ROOT, encoding: 'utf8' },
    );
    return { command, args, py: candidate, available: (mods.stdout ?? '').trim().split(/\s+/).filter(Boolean) };
  }
  console.error('❌ backend:build 找不到 Python 3.12。');
  for (const problem of problems) console.error(`   ${problem}`);
  console.error('   → 用 STUDYCREW_DEV_PYTHON 指向 Python 3.12 解释器。');
  process.exit(1);
}

/** backend/ 下最新源码修改时间（用于幂等判断）。 */
function newestSourceMtime(dir) {
  let newest = 0;
  const stack = [dir];
  while (stack.length) {
    const current = stack.pop();
    for (const entry of readdirSync(current, { withFileTypes: true })) {
      if (entry.name === '__pycache__' || entry.name === 'tests') continue;
      const full = join(current, entry.name);
      if (entry.isDirectory()) {
        stack.push(full);
        continue;
      }
      if (!/\.(py|md|json|txt)$/.test(entry.name)) continue;
      const stamp = statSync(full).mtimeMs;
      if (stamp > newest) newest = stamp;
    }
  }
  return newest;
}

const { command, args, py, available } = selectPython();

if (!available.includes('PyInstaller')) {
  console.error(`❌ backend:build 的 ${py} 未安装 PyInstaller。`);
  console.error('   → 执行：pip install pyinstaller');
  process.exit(1);
}

console.log(`[backend:build] 解释器 ${py}`);
console.log(`[backend:build] 可选依赖可用：${available.filter((m) => m !== 'PyInstaller').join(', ') || '（无）'}`);
for (const optional of ['playwright', 'ddgs']) {
  if (!available.includes(optional)) {
    console.warn(`[backend:build] ⚠ ${optional} 未安装 → 该能力不会进入产物（应用内会给出安装提示）`);
  }
}

if (!existsSync(SPEC)) {
  console.error(`❌ backend:build 找不到 spec：${relative(ROOT, SPEC)}`);
  process.exit(1);
}

if (!force && existsSync(EXE) && statSync(EXE).mtimeMs > newestSourceMtime(BACKEND)) {
  console.log(`[backend:build] 幂等跳过：产物比 backend/ 源码新（--force 可强制重建）`);
  console.log(`[backend:build] 产物：${relative(ROOT, EXE)}`);
  process.exit(0);
}

console.log('[backend:build] 运行 PyInstaller（可能需要几分钟）…');
const result = spawnSync(
  command,
  [
    ...args,
    '-m',
    'PyInstaller',
    '--noconfirm',
    '--clean',
    '--distpath',
    join(ROOT, 'dist'),
    '--workpath',
    join(ROOT, 'build', 'pyinstaller'),
    SPEC,
  ],
  { cwd: BACKEND, stdio: 'inherit', env: { ...process.env, PYTHONPATH: BACKEND } },
);

if (result.error) {
  console.error(`❌ backend:build PyInstaller 启动失败：${result.error.message}`);
  process.exit(1);
}
if (result.status !== 0) {
  console.error(`❌ backend:build PyInstaller 退出码 ${result.status}`);
  process.exit(result.status ?? 1);
}

// 产物契约：exe 必须在，且名字必须与 main.ts / electron-builder.yml 同源。
if (!existsSync(EXE)) {
  console.error(`❌ backend:build 未产出预期产物：${relative(ROOT, EXE)}`);
  console.error('   → 检查 spec 的 EXE/COLLECT name 是否被改动。');
  process.exit(1);
}

console.log(`✅ backend:build 完成：${relative(ROOT, EXE)}`);
