# 阶段 0 验收报告：交付入场券

- 日期：2026-09-16
- 仓库：`D:\agentwork\StudyCrew-worktree`，分支 `feature/studycrew-foundation`
- 起始版本：`fcbfd38`
- 报告性质：**自审报告 + 一轮独立复审后的修订版**。按项目既有流程，本阶段仍需**再审**才能标记 PASS（见 §9）。
- 本文已按独立复审（`PHASE0-REREVIEW-2026-09-16.md`）的失实清单逐条更正。**修订记录见 §10**——第一版报告有 7 处与事实不符，其中一条根因分析是错的。

---

## 1. 结论

阶段 0 的两项目标达成：

1. **该检出首次产出了可安装、可启动的 Windows NSIS 安装包**，且安装/启动/卸载全链路在本轮修复后**重新实测通过**。
   - `release/StudyCrew-Setup-1.0.36.exe`（254.6 MiB，exit code 0，version resource 正确）
2. **验证环境偏离被消除，测试基线恢复可信**。
   - 宿主 Node 26.8.1 曾让 `preflight` 失败、前端 41 个用例假性失败；在 Node 24.9.0 下 `preflight OK`、前端 **563 passed (50 files)**。

另更正了 `task-2-report.md` 的三处失实声明，并修正了过期的 SDD 台账。

**关于"几处缺陷"的准确说法**（第一版报告此处有错，已更正）：

| 类别 | 缺陷 |
|---|---|
| **硬阻断**（缺任一个都打不出包） | ① `build/installer.nsh` 缺失且无生成者；② `node_modules/electron/dist` 缺失而 `electronDist` 指向它 |
| **静默缺陷**（能打包，但产物是错的/能力缺失） | ③ spec 版本资源静默全空（错误类名 + 宽泛 `except` 吞异常）；④ 品牌迁移未覆盖打包路径 |
| **卫生问题**（不阻断也不影响现有产物） | ⑤ `hiddenimports` 含已移除的 `dotenv` 与不可解析的 `ddgs`/`duckduckgo_search` |

> **更正**：第一版报告称 ⑤ 会让"构建无法启动"。**这是错的**——实测 PyInstaller 6.13.0 对缺失的 `hiddenimport` 只打印 `ERROR: Hidden import 'x' not found` 后继续，退出码仍为 0（证据见 §5.6）。它不会阻断构建，但会让产物静默缺能力（`playwright`/`yaml` 在 `hiddenimports` 里），因此仍然必须修。

---

## 2. 交付物

| 产物 | 路径 | 说明 |
|---|---|---|
| Windows 安装包 | `release/StudyCrew-Setup-1.0.36.exe` | 254.6 MiB，未签名（受控试用件）|
| 块映射 | `release/StudyCrew-Setup-1.0.36.exe.blockmap` | electron-builder 生成 |
| 免安装目录 | `release/win-unpacked/` | 905 MiB，实测可直接启动 |
| PyInstaller 产物 | `dist/StudyCrewBackend/` | **417 MiB**（含 ms-playwright 267 MiB；exe 本身 12.34 MiB）|
| 打包契约测试 | `scripts/packaging-contract.test.mjs` | **11** 个断言 |
| PyInstaller 驱动 | `scripts/backend-build.mjs` | `npm run backend:build` |
| NSIS 资源生成器 | `scripts/make-build-resources.mjs` | `npm run build:resources` |
| Node 24 环境脚本 | `.tools/node24.ps1` | 下载并前置 Node 24.9.0（**已入库**）|
| 重命名后的 spec | `backend/StudyCrewBackend.spec` | 原 `KnoweBackend.spec` |
| 绕过实验记录 | `.tools/bypass-probe.mjs` | 自证断言加固有效（见 §5.7）|

> **更正**：第一版写 `dist/StudyCrewBackend` 为 "150 MB"。实测 **417 MiB**（其中 `ms-playwright` 267.3 MiB）。尺寸口径统一为 MiB。

---

## 3. 修复的缺陷

### 3.1 `build/installer.nsh` 缺失且无生成者（硬阻断）

`electron-builder.yml:69` 的 `nsis.include` 是硬引用，而 `build/` 被 `.gitignore` 整体忽略，全仓（含上游 `main` 的历史）从无该文件。

- **修法**：新增 `scripts/make-build-resources.mjs` 生成它，接入 `dist:win`。**不提交静态文件**——`build/` 无法进版本库，提交了也会在每个新检出丢失。
- **宏名正确性**：`customInstall` / `customUnInstall` / `customRemoveFiles` 经核实是 electron-builder 官方扩展点（`node_modules/app-builder-lib/templates/nsis/`：`installSection.nsh:81-83`、`uninstaller.nsh:156-158,161-162`）。
- **保留策略（显式决策）**：保留 `$INSTDIR\data`（用户数据，`main.ts:95`）与 `$INSTDIR\Logs`（诊断日志，`main.ts:461-463`）。两者都**不是遗漏**——第一版只保留 `data` 而漏了 `Logs`，已修，并加断言守卫。
- **已知取舍**：定义 `customRemoveFiles` 会替换 electron-builder 默认的 `un.atomicRMDir`（升级失败可回滚）。**升级中途失败不再可回滚**，这是"绝不误删用户数据"的代价，已写入 nsh 注释。

### 3.2 `node_modules/electron/dist` 缺失（硬阻断）

`electron-builder.yml:29` 设了 `electronDist: node_modules/electron/dist`（为绕过腾讯管家锁文件），但该目录与 `path.txt` 都不存在。

- **修法**：`node install.js` 下载 Electron 43.4.0 win32-x64（110 MB）。
- **前置条件已写入 `TECH.md`/`TECH.zh-CN.md`**（第一版声称"已记录"但实际没有，已更正）。

### 3.3 版本资源静默全空（静默缺陷）

spec 写的是 `FixedFileVersion`，而 PyInstaller 6.13.0 **不存在**这个类（正确名 `FixedFileInfo`）；`ImportError` 被 `except Exception: _VERSION = None` 吞掉。

- **证据**：修复前 `[System.Diagnostics.FileVersionInfo]::GetVersionInfo()` 的字段全空，构建日志无 `Copying version information to EXE`。
- **修法**：改用 `FixedFileInfo(filevers=(1,0,36,0), prodvers=(1,0,36,0))`；把宽泛 `except Exception` 换成 `if os.name == "nt":`，让导入失败**直接中断构建**。
- **验证**：修复后日志出现 `Copying version information to EXE`，7 个字段全部正确（§5.3）；并新增**产物级断言**（读真实 exe）。

### 3.4 品牌迁移未覆盖打包路径（静默缺陷）

`git grep -o KnoweBackend fcbfd38`（区分大小写）= **22** 处，涉及 **8** 个文件：spec、`electron-builder.yml`、`electron/main.ts`、`scripts/copy-chromium.mjs`、`scripts/clean-electron-build.mjs`、`backend/config.py`、`TECH.md`、`TECH.zh-CN.md`。

- **修法**：统一改为 `StudyCrewBackend`，并用测试锁死关键位置同源。
- 排版/尺寸口径同时统一：spec 品牌与版本从 `Knowe / 1.0.25.2` 更新为 `StudyCrew 学伴团 / 1.0.36.0`。

> **更正**：第一版写"21 处 / 5 个文件"，实际 22 处 / 8 个文件。

### 3.5 `hiddenimports` 卫生问题（不阻断构建）

`ddgs`、`duckduckgo_search` 在本机未安装，`dotenv` 已在 Task 2 从凭据路径移除。三者在代码里都是**故意的延迟/可选导入**（`web_tools.py:63-67`、`browser_tools.py:82-84`、`file_ops.py:326`）。

- **修法**：从 `hiddenimports` 移除三者，只保留真实存在且必须显式声明的 `playwright*` 与 `yaml`。
- **并加硬前置**：`backend:build` 现在对 `playwright`/`yaml` 缺失**直接失败**（因为它们在 `hiddenimports` 里，缺失会被 PyInstaller 静默跳过）；`ddgs` 仍只 warn。
- **代价（已记录）**：未装 `ddgs` 时产物不含网络搜索能力。

### 3.6 `copy-chromium.mjs` 的解释器探测（卫生问题）

原用裸 `python`/`python3`，与 `preflight`/`backend-build` 的 `py -3.12` 不一致；装了多个解释器时会读到**另一个** playwright 的 revision，拷入错误内核。

- **修法**：统一候选顺序并使用 `parsePythonCommand`（含空格路径不再被拆坏），**加 3.12 闸门**并打印被拒原因，不再静默回落。

---

## 4. 测试与门禁

`scripts/packaging-contract.test.mjs` —— **11 个断言**（先写红测；初次运行 `1 pass / 4 fail`）：

1. spec 的每个 `hiddenimport` 都可解析 **且列表非空**
2. spec 不得要求已移除的 `dotenv`
3. spec 品牌标识不得残留 Knowe（基于去注释文本）
4. spec 版本资源必须与 `package.json` 同源（显示串 **+ `filevers`/`prodvers` 数值元组**）
5. `electron-builder` 的 `buildVersion` 必须与 `package.json` 同源
6. `electron-builder` 引用的构建资源必须存在或有生成者（含 `Logs` 去留与禁删 `$INSTDIR`）
7. spec 必须用正确的 PyInstaller 版本 API，且**无 `except Exception`／裸 `except`／`except BaseException`**，且 `EXE(...)` 必须**接 `version=_VERSION`**
8. spec 的版本对象必须能构造成功（**从 spec 提取真实代码执行**，非硬编码副本）
9. **已构建的后端产物必须带正确版本资源**（读真实 exe 的 `FileVersionInfo`）
10. `dist:win` 必须先生成 NSIS 资源与后端产物
11. `verify:all` 必须包含打包契约测试

`verify-runtime.mjs` 新增 4 条打包产物同源断言。

### 4.1 断言加固：为什么第一版是脆弱的

独立复审用 5 种写法击穿了第一版断言（空转通过）。已加固，并用 `.tools/bypass-probe.mjs` **自证**：

| 变异 | 第一版 | 加固后 |
|---|---|---|
| 清空 `hiddenimports` | 空转通过 | ✅ 被拦住 |
| `filevers/prodvers` 清零 | 全部通过 | ✅ 被拦住 |
| 删除 `EXE(version=_VERSION)` 接线 | 全部通过 | ✅ 被拦住 |
| 宽泛 `except Exception` | 全部通过 | ✅ 被拦住 |
| 版本号滞后一版 | 全部通过 | ✅ 被拦住 |
| 删除版本对象 | — | ✅ 被拦住（提取执行通道）|

加固手段：去注释文本 + 单双引号都认 + 提取型断言必须非空 + 从 spec 真实提取执行 + 引入产物级断言。

---

## 5. 实测证据

### 5.1 环境与既有门禁（Node 24.9.0）

| 检查 | 结果 |
|---|---|
| `npm run preflight` | ✅ preflight OK |
| `npm run lint` | ✅ 0 warning |
| `npx vitest run` | ✅ **563 passed (50 files)** |
| `npm run test:foundation` | ✅ **13 tests / 0 fail**（含 11 条打包契约 + verify:runtime）|
| `npm run verify:runtime` | ✅ verified |
| `npm run electron:build` | ✅ built main preview guard: OK |

> 宿主 Node 为 26.8.1（偏离基线），必须 `. .\.tools\node24.ps1` 后用 Node 24 跑。

### 5.2 完整构建

```
npm run dist:win   →  exit code 0
```

```
copying unpacked Electron  electronDist=node_modules\electron\dist
signing with signtool.exe  path=release\win-unpacked\resources\backend\StudyCrewBackend.exe
building        target=nsis file=release\StudyCrew-Setup-1.0.36.exe archs=x64 oneClick=false perMachine=false
building block map  blockMapFile=release\StudyCrew-Setup-1.0.36.exe.blockmap
```

**产物新鲜度（I1 修复点）**：

```
installer.nsh              2026/9/16 14:23:48
StudyCrew-Setup-1.0.36.exe 2026/9/16 14:29:27   ← 晚于 nsh，且 nsh 与生成器一致（--check 通过）
```

### 5.3 版本资源

安装包：

```
ProductName : StudyCrew 学伴团
FileVersion : 1.0.36.0
CompanyName : StudyCrew contributors
```

后端 exe（7/7 字段）：

```
CompanyName / FileDescription / FileVersion / InternalName /
OriginalFilename / ProductName / ProductVersion
```

### 5.4 PyInstaller 产物内部布局

| 检查 | 结果 |
|---|---|
| `_internal/backend/{souls,prompts,locales,worker_prompt.md}` | ✅ 全部到位 |
| StudyCrew 核心包 | 在 PYZ 内（`base_library.zip` 存在，EXE 已追加 PKG）|
| 产物总大小 | 417 MiB |

### 5.5 后端产物冒烟测试与端到端启动

**直接运行 `dist\StudyCrewBackend\StudyCrewBackend.exe`**（可复现命令，含完整环境变量）：

```powershell
$env:KNOWE_AGENT="fake"; $env:KNOWE_WS_HOST="127.0.0.1"
$env:KNOWE_WS_PORT="8791"; $env:KNOWE_HEALTH_PORT="8792"
$env:KNOWE_RUNTIME_TOKEN=("0"*64); $env:KNOWE_SECRET_BROKER_TOKEN=("1"*64)
.\dist\StudyCrewBackend\StudyCrewBackend.exe
```

结果：进程存活、监听 `127.0.0.1:8791` 与 `8792`、`/health` 返回
`{"ok":false,"code":"runtime_auth_required"}`（证明 WS 与认证栈活着）。

> **更正（I9）**：第一版直接写"监听 8791/8792"却没披露上面这些环境变量。仓库默认端口是 **8080/8081**（`backend/config.py:129,131`），按默认值运行不会得到 8791/8792。第一版的证据因此不可复现，现补齐。

**启动 `release\win-unpacked`**：Electron 4 个进程 + `StudyCrewBackend` 被拉起，并实际写入
`data\backend\events\__platform__.seq`、`groups\v1\`、`harness\builds\`、`runtime\`。

> 说明：本次运行留下的 `release/win-unpacked/Logs/backend_20260916.log` 是 Electron 的日志根
> （Windows 打包态 `LOG_ROOT=$INSTDIR\Logs`），也是 §3.1 发现 `Logs` 漏删的现场。

### 5.6 PyInstaller 对缺失 hiddenimport 的行为（更正第一版因果）

```
py -3.12 -m PyInstaller --hidden-import=definitely_not_a_real_module_xyz --hidden-import=yaml main.py
→  ERROR: Hidden import 'definitely_not_a_real_module_xyz' not found
→  Building COLLECT COLLECT-00.toc completed successfully
→  EXIT=0
```

**结论**：缺失 `hiddenimport` 不阻断构建（`depend/analysis.py:751` 是 `logger.error` 后 `continue`）。
第一版把它列为"足以破坏构建的缺陷之一"是错的。

### 5.7 断言加固自证

`.tools/bypass-probe.mjs` 复刻复审的击穿手法：

```
✅ M2 清空 hiddenimports: 被断言拦住
✅ M3 filevers 清零: 被断言拦住
✅ M4 删 version 接线: 被断言拦住
✅ M5 except Exception: 被断言拦住
✅ M6 版本滞后: 被断言拦住
✅ M7 无版本对象: 被断言拦住
结论：生效变异 6 个，其中被拦住 6 个。加固有效。
```

### 5.8 安装 / 卸载 / 残留（本轮重新实测，产物晚于最终 nsh）

静默安装到隔离目录，注入模拟用户数据与日志，再静默卸载：

| 检查 | 结果 |
|---|---|
| 安装器 / 卸载器 exit code | 0 / 0 |
| 应用 exe 被移除 | ✅ |
| `resources\` 被移除 | ✅ |
| `locales\` 被移除 | ✅ |
| **`data\` 目录存留** | ✅ |
| **`data\` 内容完好** | ✅ `must-survive-uninstall` |
| **`Logs\` 目录存留**（本轮新增验证） | ✅ |
| **`Logs\` 内容完好** | ✅ `diagnostic-log-should-survive` |
| **残留清单** | ✅ 只剩有意的 `data` 与 `Logs`（首次卸载曾残留 `uninstallerIcon.ico`/`version`，已修）|

---

## 6. 可复现命令

```powershell
Set-Location D:\agentwork\StudyCrew-worktree
. .\.tools\node24.ps1            # 宿主若是 Node 26 必须先做
npm run preflight
npm run test:packaging           # 11 条打包契约
npm run dist:win                 # 完整打包
Get-ChildItem .\release\StudyCrew-Setup-1.0.36.exe
```

**前置**：`pip install pyinstaller playwright ddgs`（`playwright`/`yaml` 缺失会让 `backend:build` 硬失败）。
**首次构建机**：`cd node_modules\electron; node install.js`。

---

## 7. 未做 / 已知限制

1. **安装包未签名**；自动更新仍禁用（`electron-builder.yml:78-85`）。属原 Task 6 范围。
2. **`ddgs` 未装** → 产物无网络搜索能力。`pip install ddgs` 后需 `npm run backend:build -- --force` 重建。
3. **升级路径从未被测试**。且因自定义 `customRemoveFiles`，升级中途失败**不可回滚**（§3.1）。
4. **安装目录内测过但"安装后启动"未单独验证**：本轮验证的是 `win-unpacked` 直接启动与静默安装的**文件层面**完整性。两者同一份产物，但安装目录含中文空格路径这一差异未被单独覆盖。
5. **`.tools/node24.ps1` 只解决本机复现**，未接入 CI（仓库无 `.github/workflows`）。
6. **`docs/` 未新增任何 Task 6 交付物**（威胁模型、权限矩阵、PII flow、notices 等仍为 0）。
7. **`release/` 未重建时的一致性无守护**：`packaging-contract` 的产物级断言读的是当前 `dist/`，若把旧 `release/` 与新 `dist/` 混用不会被发现（已记录，未修）。
8. **发布坐标未核实**：`electron-builder.yml` 的 `publish.owner=StudyCrew` 指向的仓库是否存在、是否为目标源，未验证。

---

## 8. 复审发现与处置对照

独立复审判定 **FAIL（0 Critical / 9 Important / 11 Minor）**。本轮处置：

| # | 发现 | 处置 |
|---|---|---|
| I1 | 交付产物早于最终 `installer.nsh`；卸载实测对象是被取代的旧产物 | ✅ 已重打包（产物 14:29:27 > nsh 14:23:48）并重新实测卸载（§5.8）|
| I2 | `customRemoveFiles` 漏删/未定义 `$INSTDIR\Logs`（win32 打包态 `LOG_ROOT` 在此） | ✅ 显式决策为保留，nsh/`electron-builder.yml` 注释同步更正，加断言 |
| I3 | 8 条断言中 3 条可被绕过（已实测 5 种写法） | ✅ 加固为 11 条，并用 bypass-probe 自证 6/6 拦住 |
| I4 | `electron-builder.yml buildVersion` 是第 3 份无守护版本副本 | ✅ 新增同源断言 |
| I5 | `backend:build` 幂等键过窄（改 spec/依赖会跳过重建） | ✅ 改为全输入指纹（含 spec、图标、根配置、解释器与依赖版本）|
| I6 | `.tools/node24.ps1` 被 ignore 且无法入库；"已写入 TECH.md"失实 | ✅ `.gitignore` 收窄为只忽略下载产物；脚本已入库；TECH.md 已补 |
| I7 | TECH 两份文档分步流程缺 `build:resources` | ✅ 已补（并补 Electron 二进制前置）|
| I8 | 新断言不在 `verify:all` | ✅ 已并入 |
| I9 | §5.4 端口证据不可复现 | ✅ 补齐完整环境变量并说明默认端口 |
| M1 | `RMDir "$INSTDIR"` 是恒定 no-op 且注释语义误读 | ✅ 删除该行并写明理由 |
| M2 | 定义 `customRemoveFiles` 会失去升级原子回滚 | ✅ 已在 nsh 注释中显式声明该取舍 |
| M3 | 根文件按扩展名白名单删，未来新扩展名会残留 | ⚠ 未修，已写入 nsh 注释提醒（并说明测试不守护覆盖度）|
| M4 | `copy-chromium` 缺 3.12 闸门、静默回落 | ✅ 已加闸门并打印被拒原因 |
| M5 | `backend-build` 自写 `splitCommand` 会拆坏含空格路径 | ✅ 改用 `parsePythonCommand`，ENOENT 计入 problems |
| M6 | `playwright`/`yaml` 缺失被 PyInstaller 静默跳过 | ✅ 改为硬前置失败（`yaml` 也已写入 TECH 前置）|
| M7 | `DeleteRegValue ...\Run` 是死代码 | ✅ 已删除 |
| M8 | `--check` 无调用者；`PYTHONPATH` 覆盖调用者值 | ✅ `--check` 保留为可选自检；PYTHONPATH 改为追加 |
| M9 | 记录不一致（417 vs 150 MiB；22 vs 21 处；`0d89903` 收尾行与报告矛盾） | ✅ 本文与 `progress.md` 已对齐；`progress.md` 收尾行已改 |
| M10 | `TECH.md` 头部仍写 Python 3.11 / Node 20+ | ✅ 改为 3.12 / 24+ |
| M11 | `skills/` 未跟踪且未被 ignore；Round 2 红测使基线含未提交内容 | ⚠ 未修（`skills/` 按计划约束不得改动/提交；基线声明限定为"已提交内容"）|

---

## 9. 下一步

本报告是**自审**，按流程仍需一次独立复审才能标记 PASS（门槛 0 Critical / 0 Important）。复核建议聚焦：

- 加固后的 11 条断言是否还有可绕过写法（尤其 `specCode` 注释剥离与"提取执行"的边界）；
- `backend-build.mjs` 新指纹是否会**误跳过**必要重建（或过度重建）；
- `installer.nsh` 的保留集（`data`/`Logs`）与 M3 未修的覆盖度风险；
- `verify:all` 并入 `test:foundation` 后是否仍自洽。

阶段 0 PASS 之后，按既定顺序进入**阶段 1：Task 2 Round 2 收口**（必修 C-R1、I-R1、I-R6、I-R8、I-R4；接口形状修 I-R2；I-R3b 与 I-R7 记为已知限制）。

---

## 10. 修订记录（第一版 → 本版）

第一版报告有 7 处与事实不符，已全部更正：

| # | 第一版声称 | 事实 |
|---|---|---|
| 1 | `hiddenimports` 缺失会让"构建无法启动"，是"足以破坏构建的缺陷" | ❌ 因果错误。PyInstaller 只打 ERROR 后继续，退出码 0 |
| 2 | Electron 二进制缺失"已在 TECH.md 记录" | ❌ TECH.md 中 0 命中；现已真正补上 |
| 3 | "监听端口 8791/8792"作为实测证据 | ❌ 未披露所需环境变量；默认端口是 8080/8081 |
| 4 | 卸载实测（§5.5）代表最终契约 | ❌ 用的是早于最终 nsh 的旧产物；现已重打包并重测 |
| 5 | `dist/StudyCrewBackend` 为 "150 MB" | ❌ 实测 417 MiB；口径已统一 |
| 6 | "21 处引用 / 5 个文件"；"4 条同源断言" | ❌ 22 处 / 8 个文件；其中 2 条只是反向断言 |
| 7 | `progress.md`（同一提交）写"6 assertions"且"安装包尚未产出" | ❌ 与验收报告自相矛盾；已对齐为 11 条并更新收尾行 |
