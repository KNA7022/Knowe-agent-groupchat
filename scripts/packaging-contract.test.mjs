/**
 * 打包契约测试 —— 保证"PyInstaller spec / electron-builder 配置 / package.json
 * 自洽，且真实产物带正确版本资源"。
 *
 * ⚠ 定位说明：spec 的**结构**检查交给 `scripts/spec-check.mjs`（AST 检查器，
 *   由 `scripts/spec-check.test.mjs` 用畸形变异验证其强度）。本文件负责：
 *     - spec 之外的配置契约（electron-builder / package.json / NSIS 脚本）；
 *     - **产物级断言**——读真实 exe 的版本资源。这才是"版本资源确实嵌入了"的
 *       唯一硬证据；AST 那些检查都只是 lint。
 *
 * 历史教训（写在这里避免重蹈）：
 *   1. 用文本正则读 spec 被独立复审连续击穿两轮 → 结构化检查一律走 AST；
 *   2. 变异测试必须驱动**真实**检查器，不能复刻断言逻辑（那是循环论证）；
 *   3. 提取型断言必须同时断言提取结果非空，否则空列表会让断言空转。
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync, spawnSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const builderPath = join(projectRoot, 'electron-builder.yml');
const packagePath = join(projectRoot, 'package.json');

const builder = readFileSync(builderPath, 'utf8');
const pkg = JSON.parse(readFileSync(packagePath, 'utf8'));

/** 版本单一来源：package.json 三段 semver → Windows 四段。 */
const THREE_PART = pkg.version;
const FOUR_PART = `${pkg.version}.0`;

/** 去掉行尾注释后再做 YAML 文本匹配，避免合法注释造成反向误报。 */
const builderCode = builder
  .split('\n')
  .map((line) => line.replace(/\s+#.*$/, ''))
  .join('\n');

/**
 * 跑 spec-check 并解析 JSON。用 spawnSync **容忍非 0 退出码**：
 * execFileSync 会在检查器报错时抛异常并让整个测试文件崩掉，使后面的断言
 * 全部成为不可达的死代码（复审实测过这个缺陷）。
 */
function runSpecCheck(args = []) {
  const r = spawnSync('node', [join(projectRoot, 'scripts', 'spec-check.mjs'), ...args], {
    cwd: projectRoot,
    encoding: 'utf8',
    env: { ...process.env, PYTHONIOENCODING: 'utf-8' },
  });
  const raw = r.stdout ?? '';
  const line = raw.trim().split('\n').filter(Boolean).pop() ?? '{}';
  try {
    const parsed = JSON.parse(line);
    return { ...parsed, exitCode: r.status };
  } catch (err) {
    return {
      ok: false,
      problems: [`spec-check 输出无法解析：${err.message}`, raw.slice(0, 300)],
      findings: {},
      exitCode: r.status,
    };
  }
}

const specCheck = runSpecCheck();

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
  assert.ok(declared.length > 0, 'hiddenimports 生效值不应为空');
  const probe = 'import importlib.util as u, sys; sys.exit(0 if u.find_spec(sys.argv[1]) else 1)';
  const missing = declared.filter(
    (name) => spawnSync('py', ['-3.12', '-c', probe, name], { cwd: projectRoot }).status !== 0,
  );
  assert.deepEqual(missing, [], `hiddenimports 引用了不可解析的模块：${missing.join(', ')}`);
});

test('spec 不得要求已被移除的 dotenv 依赖', () => {
  const declared = specCheck.findings?.hiddenimports ?? [];
  assert.ok(!declared.includes('dotenv'), 'dotenv 已从后端凭据路径移除，spec 不应再要求它');
});

// ── electron-builder 与 package.json ─────────────────────────────────────
test('electron-builder 的 buildVersion 必须与 package.json 同源', () => {
  const match = builderCode.match(/^buildVersion:\s*(\S+)\s*$/m);
  assert.ok(match, 'electron-builder.yml 必须声明 buildVersion');
  assert.equal(
    match[1],
    FOUR_PART,
    `buildVersion=${match[1]} 与 package.json ${THREE_PART} 不同源（应为 ${FOUR_PART}）`,
  );
});

test('electron-builder 引用的构建资源必须存在或有生成者', () => {
  const include = builderCode.match(/include:\s*(\S+)/);
  assert.ok(include, 'electron-builder.yml 应声明 nsis.include');
  assert.equal(include[1], 'build/installer.nsh', 'NSIS include 路径应为 build/installer.nsh');

  const generatorPath = join(projectRoot, 'scripts', 'make-build-resources.mjs');
  assert.ok(existsSync(generatorPath), '缺少 scripts/make-build-resources.mjs：installer.nsh 无生成者');
});

test('NSIS 脚本必须保留 data 与 Logs（真正的反向断言）', () => {
  const generator = readFileSync(join(projectRoot, 'scripts', 'make-build-resources.mjs'), 'utf8');
  const nsh = generator.match(/const INSTALLER_NSH = `([\s\S]*?)`;/)?.[1] ?? '';
  assert.ok(nsh.length > 0, '必须能提取出 nsh 正文（生成器结构变了就要同步本测试）');

  // 只检查"正文里出现过 data/Logs"是无效的——注释与 DetailPrint 里本来就有这两个词，
  // 而在 customRemoveFiles 里补一句 RMDir /r "$INSTDIR\data" 照样能过。复审实测过。
  // 因此改成：任何删除指令的目标都不得位于 data\ 或 Logs\ 之下。
  const deleteLines = nsh
    .split('\n')
    // 去掉 NSIS 行内注释（; 开头），再匹配 RMDir/Delete 指令
    .map((line) => line.replace(/;.*$/, '').trim())
    .filter((line) => /^(RMDir|Delete)\b/i.test(line));

  for (const line of deleteLines) {
    assert.ok(
      !/\$INSTDIR\\+(?:data|Logs)\b/i.test(line),
      `NSIS 在卸载时会删除受保护目录：${line}`,
    );
    // 删除 $INSTDIR 本体（含 /r）会连带删掉 data/ 与 Logs/。
    assert.ok(
      !/^(RMDir|Delete)\b.*\$INSTDIR"?\s*$/i.test(line),
      `NSIS 删除整个 $INSTDIR 会连带删掉受保护目录：${line}`,
    );
  }

  assert.ok(/customRemoveFiles/i.test(nsh), 'nsh 必须定义 customRemoveFiles 宏');
  // 保留策略必须显式写进 nsh 注释/输出，便于下一个人理解这是决策而非遗漏。
  assert.ok(/data/i.test(nsh) && /Logs/i.test(nsh), 'nsh 必须说明 data 与 Logs 的保留策略');
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

// ── 产物级契约（唯一的硬证据） ───────────────────────────────────────────
test('已构建的后端产物必须带正确版本资源（含 filevers/prodvers 数字字段）', (t) => {
  const exe = join(projectRoot, 'dist', 'StudyCrewBackend', 'StudyCrewBackend.exe');
  if (!existsSync(exe)) {
    // 用 t.skip 而不是静默 return：`skipped=0` 会让人误以为产物契约被验证过。
    // 注意：`verify:all` 不含打包步骤，所以这里在干净检出上会 skip —— 这是已知限制。
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

test('spec 的版本对象必须字段齐全、数字版本正确、品牌不残留 Knowe', () => {
  // 从 spec 真实切出赋值块执行（不是文本匹配），既能覆盖字段缺失，也能覆盖
  // ffi 的数字版本被清零——后者只读 FileVersionInfo 的字符串是看不出来的。
  const specSrc = readFileSync(join(projectRoot, 'backend', 'StudyCrewBackend.spec'), 'utf8');
  const lines = specSrc.split('\n');
  // 取"有效"的那次赋值：spec-check 报的 version_assign_lines 里，
  // 有效分支是构造 VSVersionInfo 的那条，else 分支是 _VERSION = None。
  const versionLines = specCheck.findings?.version_assign_lines ?? [];
  const validLines = versionLines.filter((line) =>
    (lines[line - 1] ?? '').includes('VSVersionInfo'),
  );
  assert.equal(
    validLines.length,
    1,
    `应恰好有 1 条构造 VSVersionInfo 的 _VERSION 赋值，实际 ${validLines.length} 条`,
  );
  const validLine = validLines[0];

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
  const source = block
    .join('\n')
    .split('\n')
    .map((line) => (line.startsWith('    ') ? line.slice(4) : line))
    .join('\n');
  assert.ok(source.includes('VSVersionInfo'), `切出的版本块不像版本对象：${source.slice(0, 60)}`);

  const py = [
    'import json, os, sys',
    'from PyInstaller.utils.win32.versioninfo import (',
    '    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,',
    ')',
    source,
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
    'ffi = info.ffi',
    'def quad(ms, ls):',
    '    return "%d.%d.%d.%d" % ((ms >> 16) & 0xFFFF, ms & 0xFFFF, (ls >> 16) & 0xFFFF, ls & 0xFFFF)',
    'print(json.dumps({"fields": fields, "filevers": quad(ffi.fileVersionMS, ffi.fileVersionLS),',
    '                  "prodvers": quad(ffi.productVersionMS, ffi.productVersionLS)}, ensure_ascii=False))',
  ].join('\n');
  const raw = execFileSync('py', ['-3.12', '-c', py], { cwd: projectRoot, encoding: 'latin1' });
  const parsed = JSON.parse(Buffer.from(raw.trim(), 'latin1').toString('utf8'));

  for (const name of [
    'CompanyName',
    'FileDescription',
    'FileVersion',
    'InternalName',
    'OriginalFilename',
    'ProductName',
    'ProductVersion',
  ]) {
    assert.ok(parsed.fields[name], `版本资源缺少字段 ${name}`);
  }
  assert.equal(parsed.fields.FileVersion, FOUR_PART, `FileVersion=${parsed.fields.FileVersion}`);
  assert.equal(parsed.fields.ProductVersion, FOUR_PART, `ProductVersion=${parsed.fields.ProductVersion}`);
  assert.equal(parsed.fields.InternalName, 'StudyCrewBackend', 'InternalName 必须与产物名同源');
  // 数字版本字段：只读 FileVersionInfo 的字符串是看不出来的（复审实测 filevers 清零能过）。
  assert.equal(parsed.filevers, FOUR_PART, `ffi 数字版本 filevers=${parsed.filevers}，期望 ${FOUR_PART}`);
  assert.equal(parsed.prodvers, FOUR_PART, `ffi 数字版本 prodvers=${parsed.prodvers}，期望 ${FOUR_PART}`);
  assert.ok(
    !/\bKnowe\b/.test(`${parsed.fields.CompanyName} ${parsed.fields.ProductName}`),
    `版本资源品牌仍含 Knowe：${parsed.fields.CompanyName} / ${parsed.fields.ProductName}`,
  );
});
