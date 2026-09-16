/**
 * 阶段 0 红测 —— 打包链前置契约。
 *
 * 这些断言锁定"PyInstaller spec 与 electron-builder 配置必须自洽"这条不变量：
 *   - spec 的 hiddenimports 只能包含真实存在的模块（不存在会让 PyInstaller 失败）
 *   - spec 不得要求已被移除的依赖（dotenv 已在 Task 2 从凭据路径移除）
 *   - spec 的品牌/版本必须与 package.json 同源
 *   - electron-builder.yml 引用的每个构建资源都必须有生成者
 *
 * 运行：node --test scripts/packaging-contract.test.mjs
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';

const projectRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const specPath = join(projectRoot, 'backend', 'StudyCrewBackend.spec');
const builderPath = join(projectRoot, 'electron-builder.yml');
const packagePath = join(projectRoot, 'package.json');

assert.ok(existsSync(specPath), `缺少 PyInstaller spec：${specPath}（品牌迁移后应改名）`);

const spec = readFileSync(specPath, 'utf8');
/** 静态检查必须忽略注释：修复说明里会正当地引用被修掉的错误写法。 */
const specCode = spec
  .split('\n')
  .filter((line) => !line.trimStart().startsWith('#'))
  .join('\n');
const builder = readFileSync(builderPath, 'utf8');
const pkg = JSON.parse(readFileSync(packagePath, 'utf8'));

/** 从 spec 里抽出 hiddenimports 列表的字符串字面量。 */
function specHiddenImports() {
  const block = spec.match(/hiddenimports\s*=\s*\[([\s\S]*?)\]/);
  assert.ok(block, 'spec 必须含 hiddenimports 列表');
  return [...block[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]);
}

/** 用 Python 判断一个顶层模块是否可导入。 */
function moduleResolvable(moduleName) {
  const top = moduleName.split('.')[0];
  const probe = `import importlib.util as u, sys; sys.exit(0 if u.find_spec(${JSON.stringify(top)}) else 1)`;
  const r = spawnSync('py', ['-3.12', '-c', probe], { cwd: projectRoot, encoding: 'utf8' });
  return r.status === 0;
}

test('spec 的每个 hiddenimport 都必须可解析（否则 PyInstaller 失败）', () => {
  const missing = specHiddenImports().filter((name) => !moduleResolvable(name));
  assert.deepEqual(
    missing,
    [],
    `spec hiddenimports 引用了不存在的模块：${missing.join(', ')}`,
  );
});

test('spec 不得要求已被移除的 dotenv 依赖', () => {
  assert.ok(
    !specHiddenImports().includes('dotenv'),
    'dotenv 已从后端凭据路径移除（runtime_settings.py 只在文档字符串提及），spec 不应再要求它',
  );
});

test('spec 的品牌标识不得残留上游 Knowe', () => {
  assert.ok(
    !/StringStruct\("(?:CompanyName|ProductName|FileDescription|InternalName|OriginalFilename)",\s*"[^"]*\bKnowe\b/.test(
      spec,
    ),
    'spec 的品牌字段仍写着 Knowe，品牌迁移未覆盖打包路径',
  );
});

test('spec 的版本资源必须与 package.json 同源（映射到 Windows 四段 buildVersion）', () => {
  const versions = [...spec.matchAll(/StringStruct\("(?:FileVersion|ProductVersion)",\s*"([^"]+)"\)/g)].map(
    (m) => m[1],
  );
  assert.ok(versions.length > 0, 'spec 必须声明 FileVersion/ProductVersion');

  // 单一来源：package.json 的三段 semver（显示轨）
  const three = pkg.version.split('.');
  assert.equal(three.length, 3, `package.json version 必须是三段 semver，实际为 ${pkg.version}`);
  const four = [...three, '0'].join('.');

  for (const v of versions) {
    assert.equal(
      v.split('.').slice(0, 3).join('.'),
      pkg.version,
      `spec 版本 ${v} 与 package.json ${pkg.version} 不同源（spec 严重滞后会让文件属性显示错误版本）`,
    );
    assert.equal(v.split('.').length, 4, `Windows FileVersion 必须是四段，实际为 ${v}`);
  }
  assert.ok(four.length > 0);
});

test('electron-builder 引用的构建资源必须存在或有生成者', () => {
  const include = builder.match(/include:\s*(\S+)/);
  assert.ok(include, 'electron-builder.yml 应声明 nsis.include');
  const nsh = include[1];
  assert.equal(nsh, 'build/installer.nsh', 'NSIS include 路径应为 build/installer.nsh');

  // build/ 整体被 .gitignore 忽略，所以每个新检出都必须能**重新生成**它，
  // 否则 installer.nsh 永远缺失、NSIS 打包永远失败（这正是阶段 0 修的问题）。
  assert.ok(
    existsSync(join(projectRoot, 'scripts', 'make-build-resources.mjs')),
    '缺少 scripts/make-build-resources.mjs：build/installer.nsh 无生成者，新检出无法打包',
  );

  const generator = readFileSync(join(projectRoot, 'scripts', 'make-build-resources.mjs'), 'utf8');
  assert.ok(
    generator.includes('installer.nsh'),
    'make-build-resources.mjs 必须生成 installer.nsh',
  );
  assert.ok(
    /customRemoveFiles/.test(generator),
    'installer.nsh 必须定义 customRemoveFiles（卸载时保留 $INSTDIR\\data 的依据）',
  );
  assert.ok(
    /data/.test(generator),
    'installer.nsh 生成内容必须显式涉及 data 目录保留策略',
  );
});

test('spec 必须使用正确的 PyInstaller 版本 API 且不得静默吞异常', () => {
  // 上游 bug：写了不存在的 `FixedFileVersion`，import 抛错后被
  // `except Exception: _VERSION = None` 吞掉 → exe 版本资源全空。
  // 只做正向断言：不检查 `FixedFileVersion` 字面量，因为修复说明的注释里
  // 会正当地提到它（反向断言会误伤文档）。
  assert.ok(
    /FixedFileInfo\s*\(/.test(specCode),
    'spec 必须用 FixedFileInfo(...) 构造 ffi（PyInstaller 没有 FixedFileVersion）',
  );
  assert.ok(
    /filevers\s*=\s*\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*,\s*\d+\s*\)/.test(specCode),
    'FixedFileInfo 必须传四元组 filevers',
  );
  assert.ok(
    /prodvers\s*=\s*\(\s*\d+\s*,\s*\d+\s*,\s*\d+\s*,\s*\d+\s*\)/.test(specCode),
    'FixedFileInfo 必须传四元组 prodvers',
  );
  assert.ok(
    !/except\s+Exception/.test(specCode),
    'spec 不得用宽泛的 except Exception 吞掉版本资源错误（会让 exe 静默失去版本信息）',
  );
  for (const field of [
    'CompanyName',
    'FileDescription',
    'FileVersion',
    'InternalName',
    'OriginalFilename',
    'ProductName',
    'ProductVersion',
  ]) {
    assert.ok(spec.includes(field), `spec 版本资源缺少 ${field}`);
  }
});

test('spec 的版本对象必须能在本机构造成功（端到端 API 校验）', () => {
  // 真正执行 spec 里那段构造代码，确认它在当前 PyInstaller 版本下不抛错。
  // 这能抓住"API 名字写错 + except 吞掉"这类只在打包时才暴露的静默失败。
  const script = [
    'import json, os, sys',
    'os.name = "nt"',
    'from PyInstaller.utils.win32.versioninfo import (',
    '    FixedFileInfo, StringFileInfo, StringStruct, StringTable,',
    '    VarFileInfo, VarStruct, VSVersionInfo,',
    ')',
    'info = VSVersionInfo(',
    '    ffi=FixedFileInfo(filevers=(1, 0, 36, 0), prodvers=(1, 0, 36, 0)),',
    '    kids=[StringFileInfo([StringTable("040904B0", [',
    '        StringStruct("CompanyName", "StudyCrew"),',
    '        StringStruct("ProductName", "StudyCrew")])]),',
    '        VarFileInfo([VarStruct("Translation", [0x0409, 1200])])],',
    ')',
    'print(json.dumps({"ok": True, "rendered": len(str(info))}))',
  ].join('\n');

  const r = spawnSync('py', ['-3.12', '-c', script], { cwd: projectRoot, encoding: 'utf8' });
  assert.equal(r.status, 0, `版本资源构造失败：${r.stderr || r.stdout}`);
  const parsed = JSON.parse(r.stdout.trim());
  assert.ok(parsed.ok && parsed.rendered > 0, '版本资源构造结果为空');
});

test('dist:win 必须先生成 NSIS 构建资源', () => {
  const distWin = pkg.scripts?.['dist:win'] ?? '';
  assert.ok(
    distWin.includes('build:resources'),
    'dist:win 未调用 build:resources：新检出会因缺少 build/installer.nsh 而打包失败',
  );
  assert.ok(
    distWin.indexOf('build:resources') < distWin.indexOf('electron-builder'),
    'build:resources 必须在 electron-builder 之前执行',
  );
  assert.ok(
    distWin.includes('backend:build'),
    'dist:win 未调用 backend:build：dist/StudyCrewBackend 不会存在，extraResources 取不到后端',
  );
});
