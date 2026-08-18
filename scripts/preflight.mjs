/** Startup/build preflight: pinned runtimes, dependencies, required sources, and Python import smoke. */
import { existsSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { delimiter, dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parsePythonCommand } from './python-command.mjs';

const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const requiredNodeMajor = 24;
const requiredPython = [3, 12];
const required = [
  ['package.json', resolve(projectRoot, 'package.json')],
  ['node_modules', resolve(projectRoot, 'node_modules')],
  ['backend/workspace_layout.py', resolve(projectRoot, 'backend/workspace_layout.py')],
  ['backend/server.py', resolve(projectRoot, 'backend/server.py')],
  ['backend/engine.py', resolve(projectRoot, 'backend/engine.py')],
  ['electron/main.ts', resolve(projectRoot, 'electron/main.ts')],
];
const missing = required.filter(([, path]) => !existsSync(path));
if (missing.length) {
  for (const [name, path] of missing) console.error(`❌ preflight: 缺少 ${name}（期望位置：${path}）`);
  if (missing.some(([name]) => name === 'node_modules')) console.error('   → 先跑 `npm install` 再启动。');
  process.exit(1);
}

const nodeMajor = Number.parseInt(process.versions.node.split('.')[0] ?? '', 10);
if (nodeMajor !== requiredNodeMajor) {
  console.error(`❌ preflight: 需要 Node.js ${requiredNodeMajor}.x，当前为 ${process.versions.node}。`);
  process.exit(1);
}

const candidates = [
  process.env.STUDYCREW_DEV_PYTHON,
  process.env.KNOWE_DEV_PYTHON,
  process.platform === 'win32' ? 'py -3.12' : 'python3.12',
  process.platform === 'win32' ? 'python' : 'python3',
]
  .filter((value, index, all) => value && all.indexOf(value) === index);
let smoke = null;
for (const executable of candidates) {
  const { command, args: commandArgs } = parsePythonCommand(executable);
  const version = spawnSync(command, [...commandArgs, '-c', 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'], {
    cwd: projectRoot,
    encoding: 'utf8',
  });
  if (version.error?.code === 'ENOENT') continue;
  if (version.status !== 0 || version.stdout.trim() !== requiredPython.join('.')) continue;

  const result = spawnSync(command, [...commandArgs, '-c', 'import backend.server; import backend.engine'], {
    cwd: projectRoot,
    env: {
      ...process.env,
      PYTHONPATH: [projectRoot, resolve(projectRoot, 'backend'), process.env.PYTHONPATH].filter(Boolean).join(delimiter),
      KNOWE_RUNTIME_TOKEN: process.env.KNOWE_RUNTIME_TOKEN || '0'.repeat(64),
    },
    encoding: 'utf8',
  });
  if (result.error?.code === 'ENOENT') continue;
  smoke = { executable, result };
  break;
}
if (!smoke) {
  console.error('❌ preflight: 找不到 Python 3.12；请用 STUDYCREW_DEV_PYTHON 指向 Python 3.12。');
  process.exit(1);
}
if (smoke.result.status !== 0) {
  console.error(`❌ preflight: Python import smoke 失败（${smoke.executable}）`);
  process.stderr.write(smoke.result.stderr || smoke.result.stdout || 'unknown error\n');
  process.exit(smoke.result.status || 1);
}

console.log('✅ preflight OK');
