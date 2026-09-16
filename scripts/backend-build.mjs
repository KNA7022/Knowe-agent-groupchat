#!/usr/bin/env node
/**
 * backend-build.mjs — 用 PyInstaller 打包 Python 后端
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
 *   PyInstaller 只打包**构建时可导入**的东西，所以构建机装了什么，产物就带什么。
 *
 *   ⚠ 更正一处曾被写错的因果：spec 的 hiddenimports 里有没有这两个名字，
 *   **并不决定它们是否进产物**——`playwright` 与 `yaml` 的导入在字节码里静态可见
 *   （browser_tools.py 的 `from playwright import async_api`、file_ops.py 的
 *   `import yaml`），PyInstaller 会自动收集；playwright 还自带 PyInstaller 钩子。
 *   把它们写进 hiddenimports 只是为了"显式声明"，不是唯一来源。
 *   真正的约束是：**构建机必须能 import 它们**。
 *
 *   因此这里的检查按依赖性质分级：
 *     - playwright：应用的核心浏览器能力，缺失即硬失败（避免产出残废包）；
 *     - yaml：file_ops.py 里明确标注的"可选依赖：装了就查，没装就跳过"
 *       （见该文件 _check_yaml），缺失只 warn，不为一个可选校验能力阻断整条构建链；
 *     - ddgs / duckduckgo_search：网络搜索后端，缺失只 warn。
 *
 * 幂等：不只是"产物比 backend 源码新"。打包输入还包括 spec 本身、图标、
 *   package.json、electron-builder.yml，以及 Python 解释器与关键依赖的指纹。
 *   任一变化都会触发重建——否则改完 spec 重打包会静默沿用旧 exe（与"版本资源
 *   静默失败"是同一类错误）。可用 --force 强制重建。
 */

import { createHash } from 'node:crypto';
import { existsSync, readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs';
import { delimiter, dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { parsePythonCommand } from './python-command.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const BACKEND = join(ROOT, 'backend');
const SPEC = join(BACKEND, 'StudyCrewBackend.spec');
const DIST = join(ROOT, 'dist', 'StudyCrewBackend');
const EXE = join(DIST, process.platform === 'win32' ? 'StudyCrewBackend.exe' : 'StudyCrewBackend');
const STAMP = join(ROOT, 'build', 'pyinstaller-backend-build.json');
const force = process.argv.includes('--force');

/** 缺失即硬失败：应用的核心能力，缺了产物是残废的。 */
const REQUIRED_OPTIONAL = ['playwright'];
/** 缺失只警告：真可选（file_ops 的 yaml 校验、web_tools 的 ddgs 搜索后端）。 */
const SOFT_OPTIONAL = ['yaml', 'ddgs', 'duckduckgo_search'];

/** Python 候选，与 preflight.mjs 同顺序（显式覆盖优先）。 */
const PYTHON_CANDIDATES = [
  process.env.STUDYCREW_DEV_PYTHON,
  process.env.KNOWE_DEV_PYTHON,
  process.platform === 'win32' ? 'py -3.12' : 'python3.12',
  process.platform === 'win32' ? 'python' : 'python3',
].filter((value, index, all) => value && all.indexOf(value) === index);

/** 选一个能跑起来、版本为 3.12、且装了 PyInstaller 的解释器。 */
function selectPython() {
  const problems = [];
  for (const candidate of PYTHON_CANDIDATES) {
    // 必须用 parsePythonCommand：含空格的显式路径（C:\Program Files\...）不能被按空白拆开。
    const { command, args } = parsePythonCommand(candidate);
    const probe = spawnSync(
      command,
      [...args, '-c', 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")'],
      { cwd: ROOT, encoding: 'utf8' },
    );
    if (probe.error?.code === 'ENOENT') {
      problems.push(`${candidate}: 找不到可执行文件（含空格路径需用 STUDYCREW_DEV_PYTHON 且不要加引号）`);
      continue;
    }
    if (probe.status !== 0) {
      problems.push(`${candidate}: 无法执行（退出码 ${probe.status}）`);
      continue;
    }
    const version = probe.stdout.trim();
    if (version !== '3.12') {
      problems.push(`${candidate}: 版本 ${version}，需要 3.12`);
      continue;
    }
    const mods = spawnSync(
      command,
      [
        ...args,
        '-c',
        'import importlib.util as u;print(" ".join(m for m in ("PyInstaller","playwright","yaml","ddgs","duckduckgo_search") if u.find_spec(m)))',
      ],
      { cwd: ROOT, encoding: 'utf8' },
    );
    return {
      command,
      args,
      py: candidate,
      available: (mods.stdout ?? '').trim().split(/\s+/).filter(Boolean),
    };
  }
  console.error('❌ backend:build 找不到 Python 3.12。');
  for (const problem of problems) console.error(`   ${problem}`);
  console.error('   → 用 STUDYCREW_DEV_PYTHON 指向 Python 3.12 解释器。');
  process.exit(1);
}

const WATCHED_EXTENSIONS = /\.(py|spec|md|json|txt|ini|cfg|toml)$/;

/** backend/ 下所有打包输入的"路径@mtime@size"清单。 */
function backendInputs() {
  const entries = [];
  const stack = [BACKEND];
  while (stack.length) {
    const current = stack.pop();
    for (const entry of readdirSync(current, { withFileTypes: true })) {
      if (entry.name === '__pycache__' || entry.name === 'tests') continue;
      const full = join(current, entry.name);
      if (entry.isDirectory()) {
        stack.push(full);
        continue;
      }
      if (!WATCHED_EXTENSIONS.test(entry.name)) continue;
      const st = statSync(full);
      entries.push(`${relative(ROOT, full)}@${Math.floor(st.mtimeMs)}@${st.size}`);
    }
  }
  return entries.sort();
}

/** 打包输入指纹：backend 源码 + spec + 图标 + 根配置 + 解释器与依赖版本。 */
function buildFingerprint(python) {
  const parts = backendInputs();
  for (const extra of [
    'package.json',
    'electron-builder.yml',
    join('build', 'icon.ico'),
    join('build', 'installer.nsh'),
  ]) {
    const full = join(ROOT, extra);
    if (!existsSync(full)) {
      parts.push(`${extra}@missing`);
      continue;
    }
    const st = statSync(full);
    parts.push(`${extra}@${Math.floor(st.mtimeMs)}@${st.size}`);
  }
  // 解释器与关键依赖版本：装了新 playwright / 换了 yaml 都必须重建。
  const versionProbe = spawnSync(
    python.command,
    [
      ...python.args,
      '-c',
      'import importlib.metadata as m\n' +
        'for n in ("pyinstaller", "playwright", "PyYAML", "ddgs", "duckduckgo-search"):\n' +
        '    try: print(n + "=" + m.version(n))\n' +
        '    except Exception: print(n + "=absent")\n',
    ],
    { cwd: ROOT, encoding: 'utf8' },
  );
  parts.push(`python=${python.py}`);
  parts.push((versionProbe.stdout ?? '').trim());
  return createHash('sha256').update(parts.join('\n')).digest('hex');
}

function readStamp() {
  try {
    return JSON.parse(readFileSync(STAMP, 'utf8'));
  } catch {
    return null;
  }
}

const python = selectPython();
console.log(`[backend:build] 解释器 ${python.py}`);

if (!python.available.includes('PyInstaller')) {
  console.error(`❌ backend:build 的 ${python.py} 未安装 PyInstaller。`);
  console.error('   → 执行：pip install pyinstaller');
  process.exit(1);
}

// playwright 是应用的核心浏览器能力。PyInstaller 只打包构建时可导入的东西，
// 构建机上没有它就一定不会进产物，因此这里硬失败而不是产出残废包。
const missingRequired = REQUIRED_OPTIONAL.filter((name) => !python.available.includes(name));
if (missingRequired.length) {
  console.error(`❌ backend:build 缺少必需依赖：${missingRequired.join(', ')}`);
  console.error(`   → 执行：pip install ${missingRequired.join(' ')}`);
  console.error('   （PyInstaller 只收集构建时可导入的模块，缺了它产物不会带该能力）');
  process.exit(1);
}
for (const optional of SOFT_OPTIONAL) {
  if (!python.available.includes(optional)) {
    console.warn(
      `[backend:build] ⚠ ${optional} 未安装 → 该可选能力不会进入产物（应用内会给出提示；` +
        `yaml 影响文件严格校验，ddgs 影响网络搜索）`,
    );
  }
}

if (!existsSync(SPEC)) {
  console.error(`❌ backend:build 找不到 spec：${relative(ROOT, SPEC)}`);
  process.exit(1);
}

const fingerprint = buildFingerprint(python);
const stamp = readStamp();
// fingerprint 已覆盖 backend 源码、spec、图标、根配置与依赖版本，因此
// "指纹一致 + 产物在" 就足以判定无需重建。产物 mtime 单独比较会在
// PyInstaller 输出可复现但 mtime 变化时误判，故不作为条件。
if (!force && existsSync(EXE) && stamp?.fingerprint === fingerprint) {
  console.log('[backend:build] 幂等跳过：打包输入指纹与产物一致（--force 可强制重建）');
  console.log(`[backend:build] 产物：${relative(ROOT, EXE)}`);
  process.exit(0);
}

console.log('[backend:build] 运行 PyInstaller（可能需要几分钟）…');
const result = spawnSync(
  python.command,
  [
    ...python.args,
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
  {
    cwd: BACKEND,
    stdio: 'inherit',
    env: {
      ...process.env,
      PYTHONPATH: [BACKEND, process.env.PYTHONPATH].filter(Boolean).join(delimiter),
    },
  },
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

writeFileSync(
  STAMP,
  JSON.stringify(
    {
      fingerprint,
      python: python.py,
      artifact: relative(ROOT, EXE),
      artifact_mtime_ms: Math.floor(statSync(EXE).mtimeMs),
      built_at: new Date().toISOString(),
    },
    null,
    2,
  ),
);

console.log(`✅ backend:build 完成：${relative(ROOT, EXE)}`);

// ⚠ PyInstaller 用 --noconfirm 整体替换 COLLECT 目录，会把先前 copy:chromium 拷入的
//   Playwright 浏览器内核（约 267 MiB）删掉。单独跑 `dist:win:app` 会因此静默产出
//   缺少内核的安装包（与"版本资源静默失败"同类）。这里主动提示，避免踩坑。
const chromiumRoot = join(DIST, 'ms-playwright');
const hasChromium =
  existsSync(chromiumRoot) &&
  readdirSync(chromiumRoot).some((name) => name.startsWith('chromium_headless_shell-'));
if (!hasChromium) {
  console.warn(
    '[backend:build] ⚠ 浏览器内核不在产物内（本次重建替换了整个 COLLECT 目录）。' +
      '打包前请跑 `npm run copy:chromium`，或直接用 `npm run dist:win` 走完整链；' +
      '`npm run check:artifact` 可校验产物完整性。',
  );
}
