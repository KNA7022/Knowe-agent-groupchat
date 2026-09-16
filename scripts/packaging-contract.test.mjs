/**
 * 打包契约测试 —— 锁定"PyInstaller spec / electron-builder 配置 / package.json
 * 三者必须自洽，且真实产物必须带正确版本资源"这条不变量。
 *
 * 为什么存在：这个仓库曾经"任何新检出都打不出安装包"，根因是多处互相独立的
 * 缺陷叠加。其中最隐蔽的一条是 spec 用了 PyInstaller 里不存在的
 * `FixedFileVersion`，import 抛错后被 `except Exception` 吞掉，于是产物 exe 的
 * 版本资源全是空的——构建成功、测试全绿、错误无人知晓。
 *
 * 设计原则（每一版都被独立复审击穿过，故层层加固）：
 *   1. **spec 的结构性检查一律交给 `scripts/spec-check.mjs`（Python AST）**，
 *      不再用文本正则。文本层堵不住 `except:`、`except (A, B):`、列表拼接、
 *      `_VERSION = None` 覆写、行尾注释伪造字段名这些写法。
 *   2. 提取型断言必须同时断言提取结果非空，否则空列表会让断言空转通过。
 *   3. 能读真实产物时就读产物（产物级断言），而不是只比字符串。
 *   4. 变异测试必须驱动**真实的检查器**，不能复刻一份断言逻辑（那是循环论证）。
 *      见 `scripts/spec-check.test.mjs`。
 *
 * 运行：node --test scripts/packaging-contract.test.mjs（或 npm run test:packaging）
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const specPath = join(projectRoot, 'backend', 'StudyCrewBackend.spec');
const builderPath = join(projectRoot, 'electron-builder.yml');
const packagePath = join(projectRoot, 'package.json');

assert.ok(existsSync(specPath), `缺少 PyInstaller spec：${specPath}（品牌迁移后应改名）`);

const spec = readFileSync(specPath, 'utf8');
const builder = readFileSync(builderPath, 'utf8');
const pkg = JSON.parse(readFileSync(packagePath, 'utf8'));

/** 版本单一来源：package.json 三段 semver → Windows 四段。 */
const THREE_PART = pkg.version;
const FOUR_PART = `${pkg.version}.0`;

/**
 * Python 子进程输出按 latin1 解码再转 UTF-8，避免 Windows GBK 码页控制台把
 * 中文错误信息变成 mojibake，导致关键词匹配失败。
 */
function runJson(args, extraEnv = {}) {
  const out = execFileSync('node', args, {
    cwd: projectRoot,
    encoding: 'latin1',
    env: { ...process.env, ...extraEnv },
  });
  const line = out.trim().split('\n').filter(Boolean).pop() ?? '{}';
  return JSON.parse(Buffer.from(line, 'latin1').toString('utf8'));
}

/** 用 Python 判断一个模块**全名**是否可导入（顶层校验会漏掉子模块拼写错误）。 */
function moduleResolvable(moduleName) {
  const probe =
    'import importlib.util as u, sys; sys.exit(0 if u.find_spec(sys.argv[1]) else 1)';
  const r = spawnSync('py', ['-3.12', '-c', probe, moduleName], {
    cwd: projectRoot,
    encoding: 'utf8',
  });
  return r.status === 0;
}

// ── spec 的结构性检查（AST）───────────────────────────────────────────────
const specCheck = runJson([join(projectRoot, 'scripts', 'spec-check.mjs')]);

test('spec 必须通过 AST 结构检查', () => {
  assert.equal(
    specCheck.ok,
    true,
    `spec-check 报出问题：${(specCheck.problems ?? []).join(' | ')}`,
  );
});

test('spec 的 hiddenimports 生效值必须非空且每个模块全名可解析', () => {
  const declared = specCheck.findings?.hiddenimports ?? null;
  assert.ok(Array.isArray(declared), 'spec-check 必须报告 hiddenimports 的生效常量值');
  assert.ok(
    declared.length > 0,
    'hiddenimports 生效值不应为空——空列表会让本契约测试失去意义',
  );
  const missing = declared.filter((name) => !moduleResolvable(name));
  assert.deepEqual(missing, [], `hiddenimports 引用了不可解析的模块：${missing.join(', ')}`);
});

test('spec 不得要求已被移除的 dotenv 依赖', () => {
  const declared = specCheck.findings?.hiddenimports ?? [];
  assert.ok(!declared.includes('dotenv'), 'dotenv 已从后端凭据路径移除，spec 不应再要求它');
});

test('spec 不得吞掉版本资源异常，且必须真的把版本对象接到 EXE()', () => {
  assert.deepEqual(
    specCheck.findings?.broad_excepts ?? [],
    [],
    'spec 不得使用裸 except / BaseException / Exception / ImportError 吞掉版本资源错误',
  );
  assert.equal(
    specCheck.findings?.version_wired,
    true,
    'EXE(...) 必须接 version=_VERSION，否则版本资源不会被嵌入',
  );
  // 有效赋值必须恰好一次，且出现在 EXE 之前（顺序/覆写由 spec-check 判定）。
  const assignments = specCheck.findings?.version_assignments ?? [];
  const valid = assignments.filter((a) => !a.value_is_none);
  assert.equal(valid.length, 1, `有效 _VERSION 赋值应恰好 1 次，实际 ${valid.length} 次`);
});

test('spec 的版本对象必须字段齐全且与 package.json 同源（执行 spec 真实代码）', () => {
  // 从 spec 的真实文本里切出那个有效赋值块并执行它，再检查对象结构。
  // 这样"行尾注释伪造字段名"（includes(field) 的老洞）彻底失效。
  const validLine = (specCheck.findings?.version_assignments ?? []).find((a) => !a.value_is_none)?.line;
  assert.ok(validLine, 'spec-check 必须报告有效 _VERSION 赋值所在行');
  const lines = spec.split('\n');
  let depth = 0;
  let started = false;
  const block = [];
  for (let i = validLine - 1; i < lines.length; i += 1) {
    const line = lines[i];
    block.push(line);
    for (const ch of line) {
      if (ch === '(') {
        depth += 1;
        started = true;
      } else if (ch === ')') {
        depth -= 1;
      }
    }
    if (started && depth <= 0) break;
  }
  const source = block.join('\n');
  assert.ok(source.includes('VSVersionInfo'), `切出的版本块不像版本对象：${source.slice(0, 80)}`);
  // 该赋值位于 `if os.name == "nt":` 块内（缩进 4 格），拼接进顶层脚本前必须去掉公共缩进。
  const dedented = source
    .split('\n')
    .map((line) => (line.startsWith('    ') ? line.slice(4) : line))
    .join('\n');

  const py = [
    'import json, os',
    'from PyInstaller.utils.win32.versioninfo import (',
    '    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,',
    ')',
    dedented,
    'info = _VERSION',
    'assert info is not None, "_VERSION is None"',
    'fields = {}',
    'for kid in getattr(info, "kids", []):',
    '    for table in getattr(kid, "kids", []):',
    '        for entry in getattr(table, "kids", []) or []:',
    '            name = getattr(entry, "name", None)',
    '            val = getattr(entry, "val", None)',
    '            if name is not None and val is not None:',
    '                fields[name] = val',
    'print(json.dumps(fields, ensure_ascii=False))',
  ].join('\n');
  const raw = execFileSync('py', ['-3.12', '-c', py], { cwd: projectRoot, encoding: 'latin1' });
  const fields = JSON.parse(Buffer.from(raw.trim(), 'latin1').toString('utf8'));

  for (const name of [
    'CompanyName',
    'FileDescription',
    'FileVersion',
    'InternalName',
    'OriginalFilename',
    'ProductName',
    'ProductVersion',
  ]) {
    assert.ok(fields[name], `版本资源缺少字段 ${name}（实际字段：${Object.keys(fields).join(',')}）`);
  }
  assert.equal(fields.FileVersion, FOUR_PART, `FileVersion=${fields.FileVersion}，期望 ${FOUR_PART}`);
  assert.equal(
    fields.ProductVersion,
    FOUR_PART,
    `ProductVersion=${fields.ProductVersion}，期望 ${FOUR_PART}`,
  );
  assert.equal(fields.InternalName, 'StudyCrewBackend', 'InternalName 必须与产物名同源');
  assert.equal(fields.OriginalFilename, 'StudyCrewBackend.exe', 'OriginalFilename 必须与产物名同源');
  assert.ok(
    !/\bKnowe\b/.test(fields.CompanyName + fields.ProductName),
    `版本资源品牌仍含 Knowe：${fields.CompanyName} / ${fields.ProductName}`,
  );
});

// ── electron-builder 与 package.json ─────────────────────────────────────
test('electron-builder 的 buildVersion 必须与 package.json 同源', () => {
  // buildVersion 决定安装器与 win-unpacked 的 FileVersion，是手写副本
  // （electron-builder.yml 注释自己记录过 1.0.33 漏改事故）。
  // 用 YAML 解析而非正则：行尾注释会让 `^buildVersion:\s*(\S+)\s*$` 反向误报。
  const match = builder
    .split('\n')
    .map((line) => line.replace(/\s+#.*$/, ''))
    .map((line) => line.match(/^buildVersion:\s*(\S+)\s*$/))
    .find(Boolean);
  assert.ok(match, 'electron-builder.yml 必须声明 buildVersion');
  assert.equal(
    match[1],
    FOUR_PART,
    `electron-builder.yml buildVersion=${match[1]} 与 package.json ${THREE_PART} 不同源（应为 ${FOUR_PART}）`,
  );
});

test('electron-builder 引用的构建资源必须存在或有生成者', () => {
  const cleaned = builder
    .split('\n')
    .map((line) => line.replace(/\s+#.*$/, ''))
    .join('\n');
  const include = cleaned.match(/include:\s*(\S+)/);
  assert.ok(include, 'electron-builder.yml 应声明 nsis.include');
  assert.equal(include[1], 'build/installer.nsh', 'NSIS include 路径应为 build/installer.nsh');

  const generatorPath = join(projectRoot, 'scripts', 'make-build-resources.mjs');
  assert.ok(
    existsSync(generatorPath),
    '缺少 scripts/make-build-resources.mjs：build/installer.nsh 无生成者，新检出无法打包',
  );

  const generator = readFileSync(generatorPath, 'utf8');
  // 在**生成后的脚本正文**上断言，而不是在生成器源码的注释上：
  // 注释里提到某个宏名/目录名不等于脚本真的这么写。
  const nsh = generator.match(/const INSTALLER_NSH = `([\s\S]*?)`;/)?.[1] ?? '';
  assert.ok(nsh.includes('installer.nsh') || nsh.includes('customInstall'), '生成器必须产出 nsh 正文');
  assert.ok(/!macro\s+customRemoveFiles/.test(nsh), 'nsh 必须定义 customRemoveFiles 宏');
  assert.ok(/data/.test(nsh), 'nsh 必须显式涉及 data 目录保留策略');
  assert.ok(/Logs/.test(nsh), 'nsh 必须显式声明 Logs 目录去留（win32 打包态 LOG_ROOT=$INSTDIR\\Logs）');
  // 任何删向 $INSTDIR 本体的 RMDir 都会被拦——包括 electron-builder 默认的
  // `RMDir /r $INSTDIR` 写法（旧断言只拦不带 /r 的那种，恰好放过了危险的）。
  assert.ok(
    !/RMDir\s+(?:\/[a-zA-Z]+\s+)*"?\$INSTDIR"?\s*$/m.test(nsh),
    'nsh 不得对整个 $INSTDIR 执行 RMDir（会连带删掉要保留的 data/ 与 Logs/）',
  );
});

test('dist:win 必须先生成 NSIS 构建资源与后端产物', () => {
  const distWin = pkg.scripts?.['dist:win'] ?? '';
  assert.ok(distWin.includes('build:resources'), 'dist:win 未调用 build:resources');
  assert.ok(
    distWin.indexOf('build:resources') < distWin.indexOf('electron-builder'),
    'build:resources 必须在 electron-builder 之前执行',
  );
  assert.ok(distWin.includes('backend:build'), 'dist:win 未调用 backend:build');
});

test('全量门禁必须包含打包契约', () => {
  const verifyAll = pkg.scripts?.['verify:all'] ?? '';
  assert.ok(
    /test:packaging|test:foundation/.test(verifyAll),
    'verify:all 未包含打包契约测试——跑 verify:all 变绿会让人误以为打包契约也被验证过',
  );
});

// ── 产物级契约 ───────────────────────────────────────────────────────────
test('已构建的后端产物必须带正确版本资源（产物级契约）', (t) => {
  const exe = join(projectRoot, 'dist', 'StudyCrewBackend', 'StudyCrewBackend.exe');
  if (!existsSync(exe)) {
    // 用 t.skip 而不是静默 return：`pass=11 skipped=0` 会让人以为产物契约被验证过。
    t.skip('dist/StudyCrewBackend 尚未构建');
    return;
  }
  const ps = [
    `$vi=[System.Diagnostics.FileVersionInfo]::GetVersionInfo('${exe.replace(/'/g, "''")}')`,
    '"$($vi.FileVersion)|$($vi.ProductVersion)|$($vi.CompanyName)|$($vi.InternalName)"',
  ].join('; ');
  const r = spawnSync('powershell', ['-NoProfile', '-Command', ps], { encoding: 'utf8' });
  assert.equal(r.status, 0, `读取产物版本失败：${r.stderr}`);
  const [fileVersion, productVersion, company, internal] = r.stdout.trim().split('|');
  assert.equal(fileVersion, FOUR_PART, `产物 FileVersion=${fileVersion}，期望 ${FOUR_PART}`);
  assert.equal(productVersion, FOUR_PART, `产物 ProductVersion=${productVersion}，期望 ${FOUR_PART}`);
  assert.equal(company, 'StudyCrew', `产物 CompanyName=${company}，品牌迁移未覆盖产物`);
  assert.equal(internal, 'StudyCrewBackend', `产物 InternalName=${internal}，命名不同源`);
});
