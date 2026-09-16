/**
 * 打包契约测试 —— 锁定"PyInstaller spec / electron-builder 配置 / package.json
 * 三者必须自洽，且真实产物必须带正确版本资源"这条不变量。
 *
 * 为什么存在：这个仓库曾经"任何新检出都打不出安装包"，根因是五处互相独立的
 * 缺陷叠加（详见 .superpowers/PHASE0-ACCEPTANCE-2026-09-16.md）。其中最隐蔽的
 * 一条是 spec 用了 PyInstaller 里不存在的 `FixedFileVersion`，import 抛错后被
 * `except Exception` 吞掉，于是产物 exe 的版本资源全是空的——构建成功、测试
 * 全绿、错误无人知晓。
 *
 * 本文件的设计原则（第一版被独立复审用 5 种写法绕过，故有下面的加固）：
 *   1. 静态检查一律基于**去注释文本** specCode，且单双引号都要认；
 *   2. 每个"提取型"断言都必须同时断言提取结果非空，否则空列表会让断言空转；
 *   3. 能读真实产物时就读产物（产物级断言），而不是只比字符串；
 *   4. 从 spec 里**真实提取**代码执行，而不是执行一份硬编码副本。
 *
 * 运行：node --test scripts/packaging-contract.test.mjs
 *   （或 npm run test:packaging）
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

/** 版本单一来源：package.json 三段 semver → Windows 四段。 */
const THREE_PART = pkg.version;
const FOUR_PART = `${pkg.version}.0`;

/** 从 spec 里抽出 hiddenimports 列表的字符串字面量（单双引号都要认）。 */
function specHiddenImports() {
  const block = specCode.match(/hiddenimports\s*=\s*\[([\s\S]*?)\]/);
  assert.ok(block, 'spec 必须含 hiddenimports 列表');
  // 必须同时匹配单引号与双引号：只认双引号时，改成单引号就能让断言空转通过。
  return [...block[1].matchAll(/["']([^"']+)["']/g)].map((m) => m[1]);
}

/** 用 Python 判断一个顶层模块是否可导入。 */
function moduleResolvable(moduleName) {
  const top = moduleName.split('.')[0];
  const probe = `import importlib.util as u, sys; sys.exit(0 if u.find_spec(${JSON.stringify(top)}) else 1)`;
  const r = spawnSync('py', ['-3.12', '-c', probe], { cwd: projectRoot, encoding: 'utf8' });
  return r.status === 0;
}

test('spec 的每个 hiddenimport 都必须可解析', () => {
  const declared = specHiddenImports();
  // 空列表检查：否则 `hiddenimports = []` 会让本条与下一条一起空转通过。
  assert.ok(
    declared.length > 0,
    'spec 的 hiddenimports 不应为空——空列表会让本契约测试失去意义（playwright/yaml 需显式声明）',
  );
  const missing = declared.filter((name) => !moduleResolvable(name));
  assert.deepEqual(missing, [], `spec hiddenimports 引用了不存在的模块：${missing.join(', ')}`);
});

test('spec 不得要求已被移除的 dotenv 依赖', () => {
  assert.ok(
    !specHiddenImports().includes('dotenv'),
    'dotenv 已从后端凭据路径移除（runtime_settings.py 只在文档字符串提及），spec 不应再要求它',
  );
});

test('spec 的品牌标识不得残留上游 Knowe', () => {
  assert.ok(
    !/StringStruct\(\s*["'](?:CompanyName|ProductName|FileDescription|InternalName|OriginalFilename)["']\s*,\s*["'][^"']*\bKnowe\b/.test(
      specCode,
    ),
    'spec 的品牌字段仍写着 Knowe，品牌迁移未覆盖打包路径',
  );
});

test('spec 的版本资源必须与 package.json 同源', () => {
  const three = THREE_PART.split('.');
  assert.equal(three.length, 3, `package.json version 必须是三段 semver，实际为 ${THREE_PART}`);

  // 1) 显示的 StringStruct 串
  const versions = [
    ...specCode.matchAll(
      /StringStruct\(\s*["'](?:FileVersion|ProductVersion)["']\s*,\s*["']([^"']+)["']/g,
    ),
  ].map((m) => m[1]);
  assert.ok(versions.length > 0, 'spec 必须声明 FileVersion/ProductVersion');
  for (const v of versions) {
    assert.equal(v, FOUR_PART, `spec 显示版本 ${v} 与 package.json ${THREE_PART} 不同源（应为 ${FOUR_PART}）`);
  }

  // 2) ffi 的数值元组——PE 头的真实版本来自这两个元组。只查字符串字面量时，
  //    把 filevers 改成 (0,0,0,0) 仍会通过，故必须单独比对。
  for (const key of ['filevers', 'prodvers']) {
    const m = specCode.match(
      new RegExp(`${key}\\s*=\\s*\\(\\s*(\\d+)\\s*,\\s*(\\d+)\\s*,\\s*(\\d+)\\s*,\\s*(\\d+)\\s*\\)`),
    );
    assert.ok(m, `spec 必须为 ${key} 传四元组`);
    assert.equal(
      `${m[1]}.${m[2]}.${m[3]}.${m[4]}`,
      FOUR_PART,
      `spec 的 ${key}=(${m.slice(1, 5).join(',')}) 与 package.json ${THREE_PART} 不同源（应为 ${FOUR_PART}）`,
    );
  }
});

test('electron-builder 的 buildVersion 必须与 package.json 同源', () => {
  // buildVersion 决定安装器与 win-unpacked 的 FileVersion，此前是第 3 份手写副本
  // （electron-builder.yml 注释自己记录过 1.0.33 漏改事故）。
  const m = builder.match(/^buildVersion:\s*(\S+)\s*$/m);
  assert.ok(m, 'electron-builder.yml 必须声明 buildVersion');
  assert.equal(
    m[1],
    FOUR_PART,
    `electron-builder.yml buildVersion=${m[1]} 与 package.json ${THREE_PART} 不同源（应为 ${FOUR_PART}）`,
  );
});

test('electron-builder 引用的构建资源必须存在或有生成者', () => {
  const include = builder.match(/include:\s*(\S+)/);
  assert.ok(include, 'electron-builder.yml 应声明 nsis.include');
  assert.equal(include[1], 'build/installer.nsh', 'NSIS include 路径应为 build/installer.nsh');

  const generatorPath = join(projectRoot, 'scripts', 'make-build-resources.mjs');
  assert.ok(
    existsSync(generatorPath),
    '缺少 scripts/make-build-resources.mjs：build/installer.nsh 无生成者，新检出无法打包',
  );

  const generator = readFileSync(generatorPath, 'utf8');
  assert.ok(generator.includes('installer.nsh'), 'make-build-resources.mjs 必须生成 installer.nsh');
  assert.ok(
    /customRemoveFiles/.test(generator),
    'installer.nsh 必须定义 customRemoveFiles（卸载时保留用户数据的依据）',
  );
  // 两个必须保留的目录：data（用户数据，main.ts:95）与 Logs（诊断日志，main.ts:461-463）。
  // 只断言 "data" 会让 Logs 漏删长期不被发现。
  assert.ok(/data/.test(generator), 'installer.nsh 生成内容必须显式涉及 data 目录保留策略');
  assert.ok(
    /Logs/.test(generator),
    'installer.nsh 生成内容必须显式声明 Logs 目录去留（win32 打包态 LOG_ROOT=$INSTDIR\\Logs）',
  );
  assert.ok(
    !/RMDir\s+\/r\s+"\$INSTDIR\\\\+Logs"/.test(generator),
    'Logs 已决定保留，就不应出现 RMDir /r "$INSTDIR\\Logs"',
  );
  // 禁止误删整个安装目录：data/ 与 Logs/ 都要保留，$INSTDIR 必然继续存在。
  assert.ok(
    !/^\s*RMDir\s+"\$INSTDIR"\s*$/m.test(generator),
    '不得对整个 $INSTDIR 执行 RMDir——那会连带删掉要保留的 data/ 与 Logs/（且在 CWD 下恒为 no-op）',
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
    !/except\s+Exception/.test(specCode),
    'spec 不得用宽泛的 except Exception 吞掉版本资源错误（会让 exe 静默失去版本信息）',
  );
  // 裸 except 与 except BaseException 同样会吞掉 ImportError，必须一起挡住。
  assert.ok(
    !/except\s+(?:BaseException\b|:)/.test(specCode),
    'spec 不得用裸 except / except BaseException 吞掉版本资源错误',
  );
  // 接线：光有 _VERSION 对象不够，必须真的传给 EXE()，否则版本资源不会被嵌入。
  assert.ok(
    /EXE\([\s\S]*?version\s*=\s*_VERSION/.test(specCode),
    'EXE(...) 必须接 version=_VERSION（缺这行等于构造了版本对象却不嵌入）',
  );
  // 字段名检查必须用去注释文本，否则在注释里写个字段名就能满足。
  for (const field of [
    'CompanyName',
    'FileDescription',
    'FileVersion',
    'InternalName',
    'OriginalFilename',
    'ProductName',
    'ProductVersion',
  ]) {
    assert.ok(specCode.includes(field), `spec 版本资源缺少 ${field}`);
  }
});

test('spec 的版本对象必须能在本机构造成功（从 spec 提取真实代码执行）', () => {
  // 关键：执行的是**从 spec 里真实提取**的代码，而不是复制的副本。
  // 早先版本硬编码了一份等价脚本，因此删掉 spec 里的版本接线它仍然报绿。
  const start = specCode.indexOf('if os.name == "nt":');
  assert.ok(start >= 0, 'spec 必须用 if os.name == "nt": 守卫版本资源');
  const end = specCode.indexOf('_VERSION = None', start);
  assert.ok(end > start, 'spec 的 Windows 分支必须赋值 _VERSION');
  const guardedBlock = specCode.slice(start, specCode.indexOf('\n', end));

  const script = [
    'import json, os',
    guardedBlock,
    'info = _VERSION',
    'assert info is not None, "Windows 分支必须产生 _VERSION（None 即静默失败回归）"',
    'rendered = str(info)',
    `assert ${JSON.stringify(FOUR_PART)} in rendered or "1, 0, 36, 0" in rendered, "版本对象内容为空或版本不对"`,
    'print(json.dumps({"ok": True, "rendered": len(rendered)}))',
  ].join('\n');

  const r = spawnSync('py', ['-3.12', '-c', script], { cwd: projectRoot, encoding: 'utf8' });
  assert.equal(r.status, 0, `从 spec 提取的版本资源构造失败：${r.stderr || r.stdout}`);
  const parsed = JSON.parse(r.stdout.trim());
  assert.ok(parsed.ok && parsed.rendered > 0, '版本资源构造结果为空');
});

test('已构建的后端产物必须带正确版本资源（产物级契约）', () => {
  const exe = join(projectRoot, 'dist', 'StudyCrewBackend', 'StudyCrewBackend.exe');
  if (!existsSync(exe)) {
    // 未构建过时不产生假绿：明确跳过并说明。
    console.log('[skip] dist/StudyCrewBackend 尚未构建，跳过产物级版本校验');
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

test('dist:win 必须先生成 NSIS 构建资源与后端产物', () => {
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

test('全量门禁必须包含打包契约（否则新保护挂在门禁之外）', () => {
  const verifyAll = pkg.scripts?.['verify:all'] ?? '';
  assert.ok(
    /test:packaging|test:foundation/.test(verifyAll),
    'verify:all 未包含打包契约测试——跑 verify:all 变绿会让人误以为打包契约也被验证过',
  );
});
