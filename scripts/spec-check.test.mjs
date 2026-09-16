/**
 * spec-check 变异测试 —— 验证 AST 检查器真能挡住"文本正则挡不住"的那些写法。
 *
 * 为什么要有这个文件：
 *   第一轮 `packaging-contract.test.mjs` 的断言被独立复审连续绕过，而我自己写的
 *   自证脚本 `.tools/bypass-probe.mjs` **复刻了一份断言逻辑**——那是循环论证。
 *   这里改为：对真实 spec 施加畸形变异，然后**调用真实的 scripts/spec-check.mjs**，
 *   看它是否拒绝。断言逻辑只有一份，无法自证自欺。
 *
 * 运行：node --test scripts/spec-check.test.mjs
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const REAL_SPEC = join(ROOT, 'backend', 'StudyCrewBackend.spec');
const CHECKER = join(ROOT, 'scripts', 'spec-check.mjs');
const real = readFileSync(REAL_SPEC, 'utf8');

/** 调用真实检查器，返回 { ok, problems, findings, exitCode }。 */
function runChecker(specSource) {
  // 检查器接受一个 spec 路径；把变异写到临时目录再由 Python 读取。
  const tmp = join(
    tmpdir(),
    `spec-mutant-${process.pid}-${Math.random().toString(36).slice(2)}.spec`,
  );
  writeFileSync(tmp, specSource, 'utf8');
  try {
    const r = spawnSync('node', [CHECKER, tmp], { cwd: ROOT, encoding: 'utf8' });
    const line = (r.stdout ?? '').trim().split('\n').filter(Boolean).pop() ?? '{}';
    const parsed = JSON.parse(line);
    return { ...parsed, exitCode: r.status };
  } finally {
    rmSync(tmp, { force: true });
  }
}

test('真实 spec 必须通过 AST 检查', () => {
  const r = runChecker(real);
  assert.equal(r.ok, true, `真实 spec 未通过：${JSON.stringify(r.problems)}`);
  assert.ok(r.findings.hiddenimports?.includes('playwright'), 'findings 应报告生效的 hiddenimports');
  assert.equal(r.findings.broad_excepts.length, 0, '真实 spec 不应有宽泛 except');
  assert.equal(r.findings.version_wired, true, '真实 spec 的 EXE 必须接 version=_VERSION');
});

/** 畸形变异：每种都必须被拒绝。第一列是名字，第二列是变异，第三列是期望命中的 problem 关键词。 */
/** 畸形变异：每种都必须被拒绝。第三列是可接受的拒绝理由关键词（任一命中即可）。 */
const mutants = [
  [
    '裸 except 吞掉版本资源（上游根因原型）',
    real.replace('if os.name == "nt":', 'try:').replace(/else:[\s\S]*?\n(?=\n)/, 'except:\n    _VERSION = None\n'),
    ['bare except'],
  ],
  [
    'except (ImportError, Exception) 元组',
    real.replace('if os.name == "nt":', 'try:').replace(/else:[\s\S]*?\n(?=\n)/, 'except (ImportError, Exception):\n    _VERSION = None\n'),
    ['ImportError'],
  ],
  [
    'except BaseException',
    real.replace('if os.name == "nt":', 'try:').replace(/else:[\s\S]*?\n(?=\n)/, 'except BaseException:\n    _VERSION = None\n'),
    ['BaseException'],
  ],
  [
    'hiddenimports 用变量拼接偷掉 playwright',
    // 顶层插入 `_X = []`（不能被缩进）→ 必须用行首锚点。
    real
      .replace(/\n(\s*)hiddenimports=\[[\s\S]*?\],/, '\n$1hiddenimports=["yaml"] + _X,')
      .replace(/\na = Analysis\(/, '\n_X = []\n\na = Analysis('),
    // 两条路都算通过：能求值就报"缺少 playwright"，不能求值就报"无法确认生效值"。
    // 关键词用 ASCII：中文在 GBK 码页的控制台下会被 mojibake，导致 includes() 匹配失败
    // （这个问题本身在复审时被当作"拒绝理由不对"暴露出来）。
    ['playwright', 'hiddenimports'],
  ],
  [
    'hiddenimports 整体清空',
    real.replace(/hiddenimports=\[[\s\S]*?\],/, 'hiddenimports=[],'),
    ['playwright'],
  ],
  [
    'EXE 之前插入 _VERSION = None 覆写（文本检查看不到）',
    real.replace('a = Analysis(', '_VERSION = None\n\na = Analysis('),
    ['_VERSION'],
  ],
  [
    'EXE 之后覆写 _VERSION = None',
    real.replace(/\ncoll = COLLECT\(/, '\n_VERSION = None\n\ncoll = COLLECT('),
    ['_VERSION'],
  ],
  [
    '删掉 version=_VERSION 接线',
    real.replace(/,\s*\n\s*version=_VERSION,/, ','),
    ['version=_VERSION'],
  ],
];

for (const [name, mutated, expectAny] of mutants) {
  test(`必须拒绝：${name}`, () => {
    assert.notEqual(mutated, real, `变异「${name}」未生效（正则没命中），无法据此判断检查器强度`);
    const r = runChecker(mutated);
    assert.equal(r.ok, false, `检查器未拒绝该变异：${name}`);
    assert.notEqual(r.exitCode, 0, `退出码应为非 0：${name}`);
    const joined = r.problems.join(' | ');
    const hit = expectAny.some((keyword) => joined.includes(keyword));
    assert.ok(hit, `拒绝理由应提到 ${expectAny.join(' 或 ')}，实际为：${joined}`);
  });
}
