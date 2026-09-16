/**
 * 一次性的绕过实验（不进仓库版本库，仅用于自证加固有效）。
 * 复刻独立复审当时用来击穿第一版断言的 5 种变异，确认现在会被拦住。
 */
import { readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const real = readFileSync(join(ROOT, 'backend', 'StudyCrewBackend.spec'), 'utf8');
const pkg = JSON.parse(readFileSync(join(ROOT, 'package.json'), 'utf8'));

/** 复刻加固后的断言逻辑（与 packaging-contract.test.mjs 同源）。 */
function specCodeOf(spec) {
  return spec
    .split('\n')
    .filter((line) => !line.trimStart().startsWith('#'))
    .join('\n');
}

function checkAll(spec) {
  const code = specCodeOf(spec);
  const FOUR = `${pkg.version}.0`;
  const out = {};

  // 断言1 + 空列表检查
  const block = code.match(/hiddenimports\s*=\s*\[([\s\S]*?)\]/);
  const names = block ? [...block[1].matchAll(/["']([^"']+)["']/g)].map((m) => m[1]) : null;
  out['1.hiddenimports 抽取非空'] = Boolean(names && names.length > 0);
  out['2.dotenv 不在列表'] = Boolean(names) && !names.includes('dotenv');

  // 断言: 品牌（用 specCode）
  out['3.品牌无 Knowe'] =
    !/StringStruct\(\s*["'](?:CompanyName|ProductName|FileDescription|InternalName|OriginalFilename)["']\s*,\s*["'][^"']*\bKnowe\b/.test(
      code,
    );

  // 断言: 显示版本 + ffi 元组
  const shown = [
    ...code.matchAll(/StringStruct\(\s*["'](?:FileVersion|ProductVersion)["']\s*,\s*["']([^"']+)["']/g),
  ].map((m) => m[1]);
  let tuplesOk = true;
  for (const key of ['filevers', 'prodvers']) {
    const m = code.match(
      new RegExp(`${key}\\s*=\\s*\\(\\s*(\\d+)\\s*,\\s*(\\d+)\\s*,\\s*(\\d+)\\s*,\\s*(\\d+)\\s*\\)`),
    );
    if (!m || `${m[1]}.${m[2]}.${m[3]}.${m[4]}` !== FOUR) tuplesOk = false;
  }
  out['4.显示版本同源'] = shown.length > 0 && shown.every((v) => v === FOUR);
  out['5.ffi 元组同源'] = tuplesOk;

  // 断言: 无宽泛 except
  out['6.无 except Exception'] = !/except\s+Exception/.test(code);
  out['7.无裸 except/BaseException'] = !/except\s+(?:BaseException\b|:)/.test(code);

  // 断言: version=_VERSION 接线
  out['8.version=_VERSION 已接线'] = /EXE\([\s\S]*?version\s*=\s*_VERSION/.test(code);

  // 9. 提取真实代码执行：_VERSION 必须非 None（这条只有真的执行 spec 代码才抓得到）
  const start = code.indexOf('if os.name == "nt":');
  let extractOk = false;
  if (start >= 0) {
    const end = code.indexOf('_VERSION = None', start);
    const guarded = code.slice(start, end > start ? code.indexOf('\n', end) : code.length);
    const py = [
      'import json, os',
      guarded,
      'assert _VERSION is not None, "_VERSION 为 None"',
      'print(json.dumps({"ok": len(str(_VERSION)) > 0}))',
    ].join('\n');
    const r = spawnSync('py', ['-3.12', '-c', py], { cwd: ROOT, encoding: 'utf8' });
    extractOk = r.status === 0 && (r.stdout ?? '').includes('"ok": true');
  }
  out['9.提取执行 _VERSION 非空'] = extractOk;

  // 10. customRemoveFiles 存在
  out['10.customRemoveFiles 存在'] = /customRemoveFiles/.test(
    readFileSync(join(ROOT, 'scripts', 'make-build-resources.mjs'), 'utf8'),
  );
  return out;
}

/** 每个变异附一个"确实改到了"的判据，避免把"变异没生效"误读成"断言有洞"。 */
const mutations = [
  ['REAL spec', real, () => true],
  // M2: 清空 hiddenimports（旧断言会与 dotenv 一起空转通过）
  [
    'M2 清空 hiddenimports',
    real.replace(/hiddenimports=\[[\s\S]*?\],/, 'hiddenimports=[],'),
    (s) => /hiddenimports=\[\]/.test(s),
  ],
  // M3: ffi 元组清零（旧断言只看字符串字面量，会漏）
  [
    'M3 filevers 清零',
    real.replace(/filevers=\(1, 0, 36, 0\)/, 'filevers=(0, 0, 0, 0)'),
    (s) => /filevers=\(0, 0, 0, 0\)/.test(s),
  ],
  // M4: 删掉 EXE() 的 version 接线（旧断言完全不看接线）
  [
    'M4 删 version 接线',
    real.replace(/,\s*\n\s*version=_VERSION,/, ','),
    (s) => !/version=_VERSION/.test(s),
  ],
  // M5: 用 `except Exception` 吞掉版本错误（旧断言的 except 检查基于去注释文本，应能拦住）
  [
    'M5 except Exception',
    real.replace(/if os\.name == "nt":/, 'try:').replace(/else:[\s\S]*?\n(?=\n)/, 'except Exception:\n    _VERSION = None\n'),
    (s) => /except Exception/.test(s),
  ],
  // M6: 显示版本号滞后一版（模拟"改了 package.json 忘了改 spec"）
  [
    'M6 版本滞后',
    real.replace(/1\.0\.36\.0/g, '1.0.35.0'),
    (s) => /1\.0\.35\.0/.test(s),
  ],
  // M7: 直接从 spec 删掉 FixedFileInfo（回退到"没有版本资源"的状态）
  [
    'M7 无版本对象',
    real.replace(/FixedFileInfo\(/g, 'MissingVersionClass('),
    (s) => /MissingVersionClass\(/.test(s),
  ],
];


const results = {};
const effective = {};
for (const [name, spec, verify] of mutations) {
  results[name] = checkAll(spec);
  effective[name] = verify(spec);
}

const checks = Object.keys(results['REAL spec']);
const names = mutations.map(([n]) => n);
console.log('check'.padEnd(32) + names.map((n) => n.slice(0, 9).padStart(11)).join(''));
for (const c of checks) {
  const row = names.map((n) => (results[n][c] ? 'PASS' : 'FAIL').padStart(11)).join('');
  console.log(c.padEnd(32) + row);
}
console.log('变异生效?'.padEnd(30) + names.map((n) => (effective[n] ? 'yes' : 'NO').padStart(11)).join(''));

console.log('\n=== 判定 ===');
let blockedCount = 0;
let coveredCount = 0;
for (const n of names) {
  if (n === 'REAL spec') continue;
  if (!effective[n]) {
    console.log(`⚠ ${n}: 变异未生效，不计入判定`);
    continue;
  }
  coveredCount += 1;
  const blocked = checks.some((c) => !results[n][c]);
  if (blocked) blockedCount += 1;
  console.log(`${blocked ? '✅' : '❌'} ${n}: ${blocked ? '被断言拦住' : '绕过了所有断言'}`);
}
console.log(
  `\n结论：生效变异 ${coveredCount} 个，其中被拦住 ${blockedCount} 个。` +
    (blockedCount === coveredCount ? '加固有效。' : '仍有可绕过项，需继续加固。'),
);
