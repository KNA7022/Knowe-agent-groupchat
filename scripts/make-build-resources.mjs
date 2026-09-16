#!/usr/bin/env node
/**
 * make-build-resources.mjs — 生成 build/ 下的 NSIS 构建资源（阶段 0 新增）
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
 *   --check 只校验（不写文件），供测试与非 Windows 构建机使用。
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
;   所以数据全部落在 $INSTDIR\\data，卸载时必须保留。
;
; 注意：下面三个宏是 electron-builder 的官方扩展点（macro 名不能改）。

!macro customInstall
  DetailPrint "StudyCrew 学伴团 已安装到 $INSTDIR"
  DetailPrint "用户数据保存在 $INSTDIR\\data（卸载时会保留）"
!macroend

!macro customRemoveFiles
  ; electron-builder 默认对 $INSTDIR 执行 RMDir /r。这里改为逐项删除并**跳过 data\\**，
  ; 让聊天记录与项目数据在卸载后仍然保留（与 nsis.deleteAppDataOnUninstall=false 一致）。
  DetailPrint "正在移除程序文件（保留 $INSTDIR\\data）…"

  RMDir /r "$INSTDIR\\resources"
  RMDir /r "$INSTDIR\\locales"
  RMDir /r "$INSTDIR\\cache"

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

  ; 只删"空"目录：data\\ 非空时自动保留，无需特判。
  RMDir "$INSTDIR"
!macroend

!macro customUnInstall
  ; per-user 安装：清理当前用户的运行痕迹，但不申请管理员、不动 HKLM。
  DeleteRegValue HKCU "Software\\Microsoft\\Windows\\CurrentVersion\\Run" "StudyCrew 学伴团"
  DetailPrint "StudyCrew 学伴团 已卸载；数据仍保留在 $INSTDIR\\data"
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
