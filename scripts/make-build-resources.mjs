#!/usr/bin/env node
/**
 * make-build-resources.mjs — 生成 build/ 下的 NSIS 构建资源
 *
 * 为什么必须由脚本生成而不是提交静态文件：
 *   `.gitignore` 整体忽略 `build/`（历史上它只放构建产物图标），所以
 *   `build/installer.nsh` 无法进版本库；而 electron-builder.yml 的
 *   `nsis.include` 是**硬引用**——文件不存在时 NSIS 打包直接失败。
 *   这就是阶段 0 之前"任何新检出都产不出安装包"的原因之一。
 *
 * 幂等：内容一致时跳过写入。
 *
 * 用法：node scripts/make-build-resources.mjs [--check]
 *   --check 只校验（不写文件），用于确认磁盘上的 nsh 与生成器同步。
 */

import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const BUILD_DIR = join(ROOT, 'build');
const NSH = join(BUILD_DIR, 'installer.nsh');
const checkOnly = process.argv.includes('--check');

const INSTALLER_NSH = `; installer.nsh — StudyCrew NSIS 自定义脚本
;
; 由 electron-builder.yml 的 nsis.include 引用。
; ⚠ 本文件由 scripts/make-build-resources.mjs 生成（build/ 被 .gitignore 忽略，
;   无法进版本库）。要改卸载/安装行为，请改那个生成脚本，不要直接改这里。
;
; 设计依据（对应 electron/main.ts）：
;   - INSTALL_ROOT = dirname(process.execPath)  → 安装目录（per-user）
;   - DATA_ROOT    = INSTALL_ROOT/data          → 聊天记录、项目、知识库、token 账本
;   - LOG_ROOT     = INSTALL_ROOT/Logs          → 运行日志（win32 打包态，main.ts:461-463）
;
; 卸载保留策略（**显式决策，不是遗漏**）：
;   保留 $INSTDIR\\data（用户数据）与 $INSTDIR\\Logs（诊断日志）——卸载后仍可取日志
;   排查问题。其余应用文件逐项删除。注意 NSIS 的 Delete **不递归**，所以目录用 RMDir /r。
;
; 已知取舍：定义 customRemoveFiles 会替换掉 electron-builder 默认的
;   un.atomicRMDir（先改名再删、安装失败可 un.restoreFiles 回滚）。因此
;   **升级中途失败不再可回滚**。这是"绝不误删用户数据"带来的代价，已明确接受。
;
; 注意：下面三个宏是 electron-builder 的官方扩展点（macro 名不能改）。

!macro customInstall
  DetailPrint "StudyCrew 学伴团 已安装到 $INSTDIR"
  DetailPrint "用户数据保存在 $INSTDIR\\data（卸载时会保留）"
!macroend

!macro customRemoveFiles
  ; electron-builder 默认对 $INSTDIR 执行 RMDir /r。这里改为逐项删除，并
  ; **不触碰 data\\ 与 Logs\\**，让用户数据与诊断日志在卸载后仍然保留
  ; （与 nsis.deleteAppDataOnUninstall=false 一致）。
  ;
  ; ⚠ 新增需清理的目录时，务必同时改对应测试：scripts/packaging-contract.test.mjs
  ;   只守护"data/Logs 被保留"与"生成器存在"，不守护新扩展名的覆盖度。
  DetailPrint "正在移除程序文件（保留 $INSTDIR\\data 与 $INSTDIR\\Logs）…"

  ; 目录：必须递归删（Delete 不递归）
  RMDir /r "$INSTDIR\\resources"
  RMDir /r "$INSTDIR\\locales"
  RMDir /r "$INSTDIR\\cache"

  ; 根目录文件：按扩展名白名单删（Electron 43 当前 19 个根文件均被覆盖）
  Delete "$INSTDIR\\*.exe"
  Delete "$INSTDIR\\*.dll"
  Delete "$INSTDIR\\*.pak"
  Delete "$INSTDIR\\*.bin"
  Delete "$INSTDIR\\*.dat"
  Delete "$INSTDIR\\*.json"
  Delete "$INSTDIR\\*.yml"
  Delete "$INSTDIR\\*.blockmap"
  Delete "$INSTDIR\\*.log"
  Delete "$INSTDIR\\LICENSE*"
  Delete "$INSTDIR\\Uninstall*.exe"
  Delete "$INSTDIR\\uninstallerIcon.ico"
  Delete "$INSTDIR\\version"

  ; 注意：这里**故意不删 $INSTDIR 本身**。我们有意保留 data\\ 与 Logs\\，
  ; 所以卸载后安装目录必然继续存在；写 RMDir "$INSTDIR" 只会是恒定 no-op
  ; （且 NSIS 卸载器的 CWD 就在 $INSTDIR，RemoveDirectory 对 CWD 会失败）。
!macroend

!macro customUnInstall
  DetailPrint "StudyCrew 学伴团 已卸载；数据仍保留在 $INSTDIR\\data，日志保留在 $INSTDIR\\Logs"
!macroend
`;

if (checkOnly) {
  if (!existsSync(NSH)) {
    console.error(`❌ make-build-resources --check: 缺少 ${NSH}`);
    process.exit(1);
  }
  if (readFileSync(NSH, 'utf8') !== INSTALLER_NSH) {
    console.error('❌ make-build-resources --check: installer.nsh 与生成器不一致（请重新生成）');
    process.exit(1);
  }
  console.log('✅ make-build-resources --check: build/installer.nsh 与生成器一致');
  process.exit(0);
}

mkdirSync(BUILD_DIR, { recursive: true });

let written = 0;
if (!existsSync(NSH) || readFileSync(NSH, 'utf8') !== INSTALLER_NSH) {
  writeFileSync(NSH, INSTALLER_NSH, { encoding: 'utf8' });
  written += 1;
}

console.log(
  written
    ? `[make-build-resources] 已生成 build/installer.nsh`
    : '[make-build-resources] 幂等跳过：build/installer.nsh 已是最新',
);
