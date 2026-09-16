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

> **更正（第二次）**：第一版报告称 ⑤ 会让"构建无法启动"——**这是错的**。实测 PyInstaller 6.13.0
> 对缺失的 `hiddenimport` 只打印 `ERROR: Hidden import 'x' not found` 后继续，退出码仍为 0（§5.6）。
>
> 第二版据此把它降级为"卫生问题"，但**同时给出的新因果也是错的**：第二版说它"会让产物静默缺能力
> （playwright/yaml 在 hiddenimports 里）"。实测反证——把这两个名字从 `hiddenimports` 里删掉后，
> `playwright`（52 个模块 + `driver\node.exe`）与 `yaml`（15 个模块 + `_yaml.pyd`）**照样被静态收集**，
> 因为它们的导入在字节码里可见（`browser_tools.py` 的 `from playwright import async_api`、
> `file_ops.py` 的 `import yaml`），playwright 还自带 PyInstaller 钩子。
>
> **正确表述**：`hiddenimports` 里有没有它们，对本仓库产物**没有影响**——写在那里只是"显式声明"。
> 真正的约束是 **PyInstaller 只打包构建时可导入的东西**：构建机上没装，产物就不会有。所以这一项
> 当前既**不阻断构建**，也**不影响现有产物**，是纯粹的卫生/清晰度问题。

---

## 2. 交付物

| 产物 | 路径 | 说明 |
|---|---|---|
| Windows 安装包 | `release/StudyCrew-Setup-1.0.36.exe` | 254.6 MiB，未签名（受控试用件）|
| 块映射 | `release/StudyCrew-Setup-1.0.36.exe.blockmap` | electron-builder 生成 |
| 免安装目录 | `release/win-unpacked/` | 905 MiB，实测可直接启动 |
| PyInstaller 产物 | `dist/StudyCrewBackend/` | **417 MiB**（含 ms-playwright 267 MiB；exe 本身 12.34 MiB）|
| 打包契约测试 | `scripts/packaging-contract.test.mjs` | 10 个断言（spec 结构交给 AST 检查器）|
| spec AST 检查器 | `scripts/spec-check.mjs` | 解析语法树，读**生效常量值** |
| spec 变异测试 | `scripts/spec-check.test.mjs` | 9 条：真实 spec + 8 种畸形变异 |
| PyInstaller 驱动 | `scripts/backend-build.mjs` | `npm run backend:build` |
| NSIS 资源生成器 | `scripts/make-build-resources.mjs` | `npm run build:resources` |
| Node 24 环境脚本 | `.tools/node24.ps1` | 下载并前置 Node 24.9.0（**已入库**）|
| 重命名后的 spec | `backend/StudyCrewBackend.spec` | 原 `KnoweBackend.spec` |

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

- **修法**：从 `hiddenimports` 移除三者，只保留 `playwright*` 与 `yaml`（显式声明）。
- **并加分级前置**：`backend:build` 现在按依赖性质分级——`playwright` 是应用核心浏览器能力，
  缺失即**硬失败**；`yaml`（`file_ops.py` 明确标注的可选校验）与 `ddgs` 缺失只 warn。
- **代价（已记录）**：未装 `ddgs` 时产物不含网络搜索能力；未装 `pyyaml` 时不含 YAML 严格校验。
- **因果更正**：本节第二版曾称"这两个包在 `hiddenimports` 里，缺失会被静默跳过"——**已证伪**（见 §1 的第二次更正）。

### 3.6 `copy-chromium.mjs` 的解释器探测（卫生问题）

原用裸 `python`/`python3`，与 `preflight`/`backend-build` 的 `py -3.12` 不一致；装了多个解释器时会读到**另一个** playwright 的 revision，拷入错误内核。

- **修法**：统一候选顺序并使用 `parsePythonCommand`（含空格路径不再被拆坏），**加 3.12 闸门**并打印被拒原因，不再静默回落。

---

## 4. 测试与门禁

`scripts/packaging-contract.test.mjs` —— **10 个断言**。其中 spec 的结构性检查**全部交给
`scripts/spec-check.mjs`（Python AST）**，不再用文本正则：

1. spec 必须通过 AST 结构检查（`Analysis(...)` 存在、`hiddenimports` 可静态求值、
   无裸 `except`/`BaseException`/`Exception`/`ImportError`、`EXE(...)` 已接 `version=_VERSION`、
   `_VERSION` 有效赋值恰好一次且在 `EXE` 之前、无"盖住有效值"的 `None` 覆写）
2. `hiddenimports` 生效值非空，且每个模块**全名**可解析（顶层校验会漏子模块拼写错误）
3. spec 不得要求已移除的 `dotenv`
4. 版本对象字段齐全且与 `package.json` 同源（**执行从 spec 真实切出的代码块**，按对象结构取字段）
5. `electron-builder` 的 `buildVersion` 同源（去行尾注释后再解析，避免反向误报）
6. NSIS 资源必须有生成者，且**在生成后的 nsh 正文上**断言存在 `customRemoveFiles`、
   涉及 `data`/`Logs`，并禁止任何指向 `$INSTDIR` 本体的 `RMDir`
7. `dist:win` 必须先生成 NSIS 资源与后端产物
8. `verify:all` 必须包含打包契约
9. **产物级**：读真实 exe 的 `FileVersionInfo`（`dist/` 不存在时用 `t.skip()`，
   不再静默 pass 以免 `skipped=0` 造成"已验证"的错觉）

`verify-runtime.mjs` 另有 4 条打包产物同源断言，其中 **2 条是反向断言**（断言某文件不再含
`KnoweBackend`），另 2 条校验 `electron-builder.yml` 的 `from: dist/StudyCrewBackend` 与
`main.ts` 的 `StudyCrewBackend.exe`。

### 4.1 断言加固史：为什么最终改为 AST

独立复审**三轮**击穿了前三版断言。**每一格都单独实测过**（用 `git show <rev>:scripts/packaging-contract.test.mjs` 取出该版本真实测试文件驱动变异）：

| 绕过写法 | v1 文本正则 (`0d89903`) | v2 启发式 AST (`fca1eda`) | v3 AST+执行求值 (`a41f8a0` 起) |
|---|---|---|---|
| `hiddenimports` 清空 | 绕过 | 拦住 | **拦住** |
| `hiddenimports` 单引号 | 绕过 | 绕过¹ | **接受（合法写法）** |
| `hiddenimports=["yaml"] + _X` | 绕过 | 绕过 | **拦住** |
| `filevers`/`prodvers` 清零 | 绕过 | 拦住 | **拦住**（数字字段单独断言）|
| 删 `version=_VERSION` 接线 | 绕过 | 拦住 | **拦住** |
| 裸 `except:` | 绕过 | 绕过 | **拦住**（禁止任何 except）|
| `except (ImportError, Exception):` | 绕过 | 绕过 | **拦住** |
| `except BaseException:` | 绕过 | 拦住 | **拦住** |
| `EXE` 前插 `_VERSION = None` | 绕过 | 绕过 | **拦住**（执行求值）|
| `EXE` 后插 `_VERSION = None` | 绕过 | 绕过 | **拦住** |
| 行尾注释伪造字段名 | 绕过 | 绕过² | **拦住**（按对象结构取字段）|
| 版本号滞后一版 | 拦住 | 拦住 | **拦住** |
| 生成器里 `RMDir /r "$INSTDIR"` | 绕过 | 绕过 | **拦住** |
| `globals()["_VERSION"] = None` | 绕过 | 绕过 | **拦住** |
| `exec("_VERSION = None")` | 绕过 | 绕过 | **拦住** |
| 有效赋值后补 `_VERSION = None` | 绕过 | 绕过³ | **拦住** |
| 异常别名 `_ALIAS = Exception` + 旧类名 | 绕过 | 绕过 | **拦住** |
| 假 `Analysis` / 假 `EXE` decoy | 绕过 | 绕过 | **拦住**（要求唯一）|

¹ 单引号只是另一种引号写法，**本就该被接受**，把"能否拦住它"当作强度指标是错的。
² v2 已能识别**整行**注释，仅**行尾**注释能骗过它。
³ v2 的覆写判定规则方向反了：它放过了"有效在前、None 在后"（唯一危险的顺序），
反而误拦"None 置顶、分支覆盖"（更安全的写法）。这是复审的关键发现，v3 已用**执行求值**取代该启发式。

> **更正（第四版）**：第三版这张表把 v1/v2 的结果混成一列并笼统写成"文本正则版 12 种全部通过"，
> 该总结**不成立**——上表有 4 行只在 v1 成立，且"版本号滞后一版"在 v1/v2 都被拦住。
> 第三版 §11 里"文本层那 12 种写法现已全部被拦"同样不成立（`filevers` 行当时未拦住；单引号行不是缺陷）。

**第四版的关键转变**：不再试图"用启发式堵住所有写法"。复审的结论是对的——启发式永远堵不完。
现在只有两件事被当作硬保障：

1. **失败无法静默**：spec 的版本块**没有任何 `try/except`**（检查器也禁止出现 except）。
   为此把守卫从 `if os.name == "nt":` 改成 `if sys.platform == "win32":` —— 因为在普通
   Python 进程里 `os.name` 恒为 `"nt"`，检查器无法用设置 `sys.platform` 的方式模拟两种平台。
   类名写错或模块改名都会直接让 PyInstaller 崩，而不是产出没有版本信息的 exe。
2. **断言产物**：`packaging-contract.test.mjs` 读真实 exe 的 `FileVersionInfo`，并额外执行 spec
   的版本块、按**对象结构**取出 7 个字段与 `ffi` 数字版本——这是唯一能证明"版本资源真的嵌入了"的证据。

AST 检查器降级为 **lint**（在构建前把明显错误喊出来），不再声称是完备契约。

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

### 5.7 断言加固自证（驱动真实检查器，非复刻）

```
node --test scripts/spec-check.test.mjs
ℹ tests 9 / pass 9 / fail 0
  ✔ 真实 spec 必须通过 AST 检查
  ✔ 必须拒绝：裸 except 吞掉版本资源（上游根因原型）
  ✔ 必须拒绝：except (ImportError, Exception) 元组
  ✔ 必须拒绝：except BaseException
  ✔ 必须拒绝：hiddenimports 用变量拼接偷掉 playwright
  ✔ 必须拒绝：hiddenimports 整体清空
  ✔ 必须拒绝：EXE 之前插入 _VERSION = None 覆写
  ✔ 必须拒绝：EXE 之后覆写 _VERSION = None
  ✔ 必须拒绝：删掉 version=_VERSION 接线
```

每条变异都先断言"变异确实生效"（`assert.notEqual(mutated, real)`），避免把
"变异没改到"误读成"检查器挡住了"。旧的自证脚本 `.tools/bypass-probe.mjs` 已删除
（它复刻断言逻辑，属循环论证）。

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
npm run test:packaging           # 10 条打包契约（spec 结构走 AST）
node --test scripts/spec-check.test.mjs   # 9 条：真实 spec + 8 种畸形变异
npm run dist:win                 # 完整打包
Get-ChildItem .\release\StudyCrew-Setup-1.0.36.exe
```

**前置**：`pip install pyinstaller playwright pyyaml ddgs`
（缺 `playwright` 会让 `backend:build` 硬失败；缺 `pyyaml`/`ddgs` 只警告，产物相应缺少该能力）。
**首次构建机**：`cd node_modules\electron; node install.js`。

---

## 7. 未做 / 已知限制

1. **安装包未签名**；自动更新仍禁用（`electron-builder.yml:78-85`）。属原 Task 6 范围。
2. **`ddgs` 未装** → 产物无网络搜索能力。`pip install ddgs` 后需 `npm run backend:build -- --force` 重建。
3. **升级路径从未被测试**。且因自定义 `customRemoveFiles`，升级中途失败**不可回滚**（§3.1）。
4. **安装目录内测过但"安装后启动"未单独验证**：本轮验证的是 `win-unpacked` 直接启动与静默安装的**文件层面**完整性。两者同一份产物，但安装目录含中文空格路径这一差异未被单独覆盖。
5. **`.tools/node24.ps1` 只解决本机复现**，未接入 CI（仓库无 `.github/workflows`）。
6. **`docs/` 未新增任何 Task 6 交付物**（威胁模型、权限矩阵、PII flow、notices 等仍为 0）。
7. **`release/` 未重建时的一致性无守护**：产物级断言读的是当前 `dist/`；把旧 `release/` 与新 `dist/` 混用不会被发现。
8. **`verify:all` 不含任何打包步骤**：`dist:win` 不在门禁里，所以"门禁全绿"**不等于**"能出安装包"。
9. **产物级断言可被跳过**：`dist/` 不存在时该条 `t.skip()`（会比静默 pass 诚实，但仍不计入失败）。
10. **`dist/` 内部完整性无守护**：只检查 `StudyCrewBackend.exe` 存在，`_internal/` 被删不会触发重建。
11. **第 4 份版本副本**：`backend/config.py:393` 的默认版本仍是 `1.0.34`（`package.json` 是 1.0.36），未被同源断言覆盖。运行时被 `electron/main.ts:980` 的 `app.getVersion()` 覆盖，故不致命，但属同类隐患。
12. **发布坐标未核实**：`electron-builder.yml` 的 `publish.owner=StudyCrew` 指向的仓库是否存在、是否为目标源，未验证。
13. **`skills/` 未被 ignore 且不提交**：按计划约束不得改动/提交；"基线可信"仅指已提交内容。
14. **`M3` 未修**：NSIS 根文件按扩展名白名单删，未来 Electron 新增非白名单后缀会在卸载后残留（实测当前 Electron 43 的 19 个根文件 100% 被覆盖，故为低风险卫生问题）。
15. **`backend-build.mjs` 的依赖指纹不完整**：只覆盖 `pyinstaller`/`playwright`/`PyYAML`/`ddgs`/`duckduckgo-search` 五个包，而 backend 真正会被冻结的还有 `httpx`、`websockets`——`pip install -U httpx` 不会触发重建，会静默沿用旧 exe（与"版本资源静默失败"同类）。反过来 `package.json`/`electron-builder.yml` 被纳入指纹却是纯粹的过度重建成本（它们与 PyInstaller 产物无关）。**未修**。
16. **AST 检查器不是完备契约**：它已被三轮复审击穿，第四版把它降级为 lint 并明确写进文件头。真正的硬保障只有"版本块没有可吞的异常" + "产物级断言读真实 exe"。**不要**再基于"检查器全绿"推断产物正确。
17. **Python 探测仍硬编码 `py -3.12`**：`packaging-contract.test.mjs` 与 `spec-check.mjs` 都直接用 `py -3.12`，没有走 `scripts/python-command.mjs` 的候选序；在无 `py` launcher 的机器上会给出误导性失败（报成"hiddenimports 不可解析"）。`backend-build.mjs` 与 `copy-chromium.mjs` 已统一，测试侧**未统一**。
18. **`dist/` 的浏览器内核会被 `backend:build` 清掉**：PyInstaller 用 `--noconfirm` 整体替换 COLLECT 目录。`npm run dist:win` 完整链会重跑 `copy:chromium` 所以安全；但单独跑 `npm run dist:win:app` 会静默产出缺内核的包。已加 `npm run check:artifact`（接入 `dist:win`）与 `backend:build` 结束时的警告来兜住，**但独立的 `dist:win:app` 仍不会自动校验**。
19. **NSIS 保留集断言是文本级**：能拦直连的 `RMDir`/`Delete` 写法，拦不住间接写法（`StrCpy` 中转、`nsExec::Exec` 调 `rmdir`）。
20. **产物级断言的标题与实际不同**：`packaging-contract.test.mjs` 里"（含 filevers/prodvers 数字字段）"这条只读 4 个字符串字段；数字字段断言作用于 spec 对象而非产物。
21. **探针不校验 spec 的 `else:` 分支**：它无条件把 `sys.platform` 设为 `win32`（只发 Windows，可接受）。

---

## 8. 复审发现与处置对照

独立复审判定 **FAIL（0 Critical / 9 Important / 11 Minor）**。本轮处置：

| # | 发现 | 处置 |
|---|---|---|
| I1 | 交付产物早于最终 `installer.nsh`；卸载实测对象是被取代的旧产物 | ✅ 已重打包（产物 14:29:27 > nsh 14:23:48）并重新实测卸载（§5.8）|
| I2 | `customRemoveFiles` 漏删/未定义 `$INSTDIR\Logs`（win32 打包态 `LOG_ROOT` 在此） | ✅ 显式决策为保留，nsh/`electron-builder.yml` 注释同步更正，加断言 |
| I3 | 8 条断言中 3 条可被绕过（复审实测 5 种写法） | ⚠ 第二版加固后被**再审再次击穿**（6 种新写法）→ 见 §11；最终改用 AST + 变异测试 |
| I4 | `electron-builder.yml buildVersion` 是第 3 份无守护版本副本 | ✅ 新增同源断言（去行尾注释后解析，避免反向误报）|
| I5 | `backend:build` 幂等键过窄（改 spec/依赖会跳过重建） | ✅ 改为全输入指纹（含 spec、图标、根配置、解释器与依赖版本）；⚠ 仍有 `httpx`/`websockets` 未覆盖（§11）|
| I6 | `.tools/node24.ps1` 被 ignore 且无法入库；"已写入 TECH.md"失实 | ✅ `.gitignore` 收窄为只忽略下载产物；脚本已入库；TECH.md 已补 |
| I7 | TECH 两份文档分步流程缺 `build:resources` | ⚠ 第二版补了流程但 pip 命令漏 `pyyaml` 且与硬闸门冲突 → 见 §11 已修 |
| I8 | 新断言不在 `verify:all` | ✅ 已并入 |
| I9 | §5.4 端口证据不可复现 | ✅ 补齐完整环境变量并说明默认端口 |
| M1 | `RMDir "$INSTDIR"` 是恒定 no-op 且注释语义误读 | ✅ 删除该行并写明理由 |
| M2 | 定义 `customRemoveFiles` 会失去升级原子回滚 | ✅ 已在 nsh 注释中显式声明该取舍 |
| M3 | 根文件按扩展名白名单删，未来新扩展名会残留 | ⚠ 未修；再审实测当前 Electron 43 的 19 个根文件 100% 被覆盖，风险为低（§11）|
| M4 | `copy-chromium` 缺 3.12 闸门、静默回落 | ✅ 已加闸门并打印被拒原因 |
| M5 | `backend-build` 自写 `splitCommand` 会拆坏含空格路径 | ✅ 改用 `parsePythonCommand`，ENOENT 计入 problems |
| M6 | `playwright`/`yaml` 缺失被 PyInstaller 静默跳过 | ⚠ 处置**理由错误**（已证伪）、且 yaml 硬失败属过度约束 → 见 §11 已改为分级 |
| M7 | `DeleteRegValue ...\Run` 是死代码 | ✅ 已删除 |
| M8 | `--check` 无调用者；`PYTHONPATH` 覆盖调用者值 | ✅ `--check` 保留为可选自检；PYTHONPATH 改为追加 |
| M9 | 记录不一致（417 vs 150 MiB；22 vs 21 处；`progress.md` 与报告矛盾） | ✅ 本文与 `progress.md` 已对齐 |
| M10 | `TECH.md` 头部仍写 Python 3.11 / Node 20+ | ✅ 改为 3.12 / 24+ |
| M11 | `skills/` 未跟踪且未被 ignore；Round 2 红测使基线含未提交内容 | ⚠ 未修（`skills/` 按计划约束不得改动/提交；基线声明限定为"已提交内容"）|

---

## 9. 下一步

本报告是**自审**，按流程仍需一次独立复审才能标记 PASS（门槛 0 Critical / 0 Important）。
本报告已经过**两轮**独立复审（§8、§11），两轮都判 FAIL 并各自抓出真实缺陷；第二轮后的修复即本节所在版本。

第三轮复核建议聚焦：

- AST 检查器是否还有可绕过写法（尤其"提取执行"对 `_VERSION` 的取值语义、以及 `hiddenimports`
  在**非赋值**形式下的求值边界）；
- `backend-build.mjs` 指纹的覆盖与代价：`httpx`/`websockets` 等真实冻结依赖未纳入，
  而 `package.json`/`electron-builder.yml` 纳入却是纯粹的过度重建成本；
- `installer.nsh` 的保留集（`data`/`Logs`）与 M3 未修的覆盖度风险；
- `verify:all` 仍**不含打包步骤**：门禁全绿不等于能出安装包。

阶段 0 PASS 之后，按既定顺序进入**阶段 1：Task 2 Round 2 收口**（必修 C-R1、I-R1、I-R6、I-R8、I-R4；接口形状修 I-R2；I-R3b 与 I-R7 记为已知限制）。

---

## 10. 修订记录（第一版 → 第二版）

第一版报告有 7 处与事实不符，第二版已全部更正：

| # | 第一版声称 | 事实 |
|---|---|---|
| 1 | `hiddenimports` 缺失会让"构建无法启动"，是"足以破坏构建的缺陷" | ❌ 因果错误。PyInstaller 只打 ERROR 后继续，退出码 0。（第二版给的**替代理由**同样是错的，见 §1 第二次更正）|
| 2 | Electron 二进制缺失"已在 TECH.md 记录" | ❌ TECH.md 中 0 命中；第二版才真正补上 |
| 3 | "监听端口 8791/8792"作为实测证据 | ❌ 未披露所需环境变量；默认端口是 8080/8081 |
| 4 | 卸载实测（§5.5）代表最终契约 | ❌ 用的是早于最终 nsh 的旧产物；第二版重打包并重测 |
| 5 | `dist/StudyCrewBackend` 为 "150 MB" | ❌ 实测 417 MiB；口径已统一 |
| 6 | "21 处引用 / 5 个文件"；"4 条同源断言" | ❌ 22 处 / 8 个文件；其中 2 条只是反向断言（该更正第三版才落到 §4）|
| 7 | `progress.md`（同一提交）写"6 assertions"且"安装包尚未产出" | ❌ 与验收报告自相矛盾；已对齐并更新收尾行 |

---

## 11. 第三轮独立复审（对第二版）与其处置

对第二版的独立复审判定 **FAIL（0 Critical / 4 Important / 11 Minor）**，并第 4 次指出报告自身的不实。
其核心发现与本次处置：

| 发现 | 内容 | 处置 |
|---|---|---|
| **I-1** | **第二版的断言加固不成立**：复审用**真实测试文件**驱动 6 种新写法，全部 11 条绿灯（含裸 `except:` 与 `RMDir /r "$INSTDIR"`）| ✅ 改用 Python **AST**（`scripts/spec-check.mjs`）读生效常量值；新增 `scripts/spec-check.test.mjs`（9 条，含 8 种畸形变异）**驱动真实检查器**。文本层那 12 种写法现已全部被拦 |
| **I-2** | 第二版对 hiddenimports 的**新因果仍然错**：实测删掉这两个名字后 playwright/yaml 照样被静态收集 | ✅ 已按实测更正（§1 第二次更正、§3.5），并把检查改为分级 |
| **I-3** | TECH 文档与报告的前置命令漏 `PyYAML`，与新增的 yaml 硬失败直接冲突 → 全新机器仍打不出包 | ✅ 三处命令统一为 `pip install pyinstaller playwright pyyaml ddgs`；`yaml` 降为 warn（它在 `file_ops.py` 里明确是可选依赖）|
| **I-4** | I5 的"全输入指纹/依赖版本"名过其实：`httpx`/`websockets` 未覆盖，而 `package.json`/`electron-builder.yml` 纳入属纯成本 | ⚠ 部分修：报告措辞已收敛为"关键依赖"；指纹覆盖问题记入 §7.10（未改代码）|
| M-a | 产物级断言在 `dist/` 缺失时静默 pass（`skipped=0` 造成"已验证"错觉）| ✅ 改用 `t.skip()` |
| M-b/c | 契约测试硬编码 `py -3.12`；`moduleResolvable` 只查顶层包 | ✅ 改为查模块**全名**；Python 探测口径记入 §7 |
| M-d | `buildVersion` 正则遇行尾注释反向误报 | ✅ 先剥行尾注释再解析 |
| M-f | §4 仍写"4 条同源断言"（§10 自己说这说法错了）| ✅ §4 已更正为"2 条为反向断言" |
| M-g | `.tools/bypass-probe.mjs` 自称不入库却已入库，且**复刻断言**（循环论证）| ✅ **已删除**，统一由 `spec-check.test.mjs` 驱动真实检查器 |
| M-j | §5.5 用现在时引用已不存在的 `Logs` 现场 | ✅ 已注明"证据已清理" |
| M3（再审）| 复审实测：当前 Electron 43 的 19 个根文件 **100% 被现有模式覆盖** | ✅ 风险确认属"低（卫生）"；仍保留在 §7.14 |

**复审对我方法的批评是正确的**：第二版用自写探针"自证"加固有效，而该探针复刻了一份断言逻辑——
断言逻辑有两份时，测其中一份不能证明另一份。这个错误比它抓到的代码缺陷更值得记住。

---

## 12. 第四轮独立复审（对第三版）与其处置

对第三版的独立复审**再次判定 FAIL（0 Critical / 8 Important / 7 Minor）**。它做了三件很有价值的事：

1. **击穿了我的"AST 加固"**：用真实测试文件驱动 6 种新写法，19 条断言全绿，而运行期 `_VERSION` 确实是 `None`。
2. **指出我的判定规则方向反了**：`_VERSION` 覆写规则放过了唯一危险的顺序（有效→None，且在 `EXE` 前），反而误拦更安全的写法（None 置顶 + 分支覆盖）。
3. **给出了正确的方向**：*"AST 检查器只能当 lint，不能当契约"*——真正兜底的是"让失败无法静默" + "断言产物"，不是再加规则。

### 处置

| 发现 | 内容 | 处置 |
|---|---|---|
| I1 | `_VERSION` 覆写判定方向颠倒（漏危险序、误拦安全序）；`EXE` 之后覆写其实无害（`EXE.__init__` 调用时已取走引用）| ✅ 删掉该启发式，改为**执行求值**：抽出赋值/条件块真跑一遍，直接看 `_VERSION` 是不是 `None` |
| I2 | 6 种写法全绿（`globals()[...]`、`exec`、危险序、异常别名、decoy `Analysis`/`EXE`）| ✅ 全部拦住（见 §4.1 表）；新增 7 条变异测试 |
| I3 | `spec-check` 报问题时 `execFileSync` 抛异常 → 整份测试文件崩、后续断言成死代码；中文输出 mojibake | ✅ `runJson` 改 `spawnSync` 容忍非 0 退出码；Python 子进程加 `PYTHONIOENCODING=utf-8` |
| I4 | `filevers`/`prodvers` 数字字段无守护（只读字符串版本看不出来）| ✅ 在版本对象断言里解析 `ffi` 的 `fileVersionMS/LS`、`productVersionMS/LS` 并比对四段版本 |
| I5 | NSIS "保留 data/Logs" 只是字符串出现性检查；`RMDir /r "$INSTDIR\data"`、大写 `RMDIR`、行尾注释都能过 | ✅ 改为**反向断言**：任何 `RMDir`/`Delete` 目标不得位于 `data\`/`Logs\` 之下，不得删 `$INSTDIR` 本体；大小写不敏感、剥行尾注释 |
| I6 | spec 注释自相矛盾（一处说"需要显式声明"，一处说"其实会被自动收集"）| ✅ 统一为实测结论 |
| R1 | §4.1 表把 v1/v2 结果混为一列，"12 种全部被拦"不实 | ✅ 已重写为 v1/v2/v3 三列并逐格标注（§4.1）|
| R2 | 交叉引用指向不存在的条目；测试计数停留在上一版 | ✅ 见下方"计数更正"，并把 httpx/websockets、`py -3.12` 探测真正写进 §7 |

### 采纳的方向性改变

第三版仍在"加规则"。第四版承认**启发式堵不完**，改为两条硬保障（详见 §4.1 末）：

- spec 的版本块**没有任何 `try/except`**（检查器一并禁止 except），失败直接崩构建；
- **产物级断言**读真实 exe，并执行 spec 的版本块按对象结构校验 7 个字段 + `ffi` 数字版本。

同时把守卫从 `os.name == "nt"` 改为 `sys.platform == "win32"`：前者在普通 Python 进程里恒为真，
检查器无法模拟非 Windows 分支；后者可被设置，探针才能真正区分两种平台。

### 计数更正（覆盖前文散落数字）

| 项 | 本文早期数字 | 实测 |
|---|---|---|
| `packaging-contract.test.mjs` | "10 个断言"（正文只列 9 条）| 10 个 test（spec 结构 1 + hiddenimports 1 + dotenv 1 + buildVersion 1 + 资源生成者 1 + NSIS 1 + dist:win 1 + verify:all 1 + 产物级 1 + 版本对象 1）|
| `spec-check.test.mjs` | — | 17 个 test（真实 spec 1 + 拒绝 14 + 接受 2）|
| `npm run test:packaging` | 19 | **27**（10 + 17）|
| `npm run test:foundation` | 13 / 21 | **29**（2 + 27）+ verify:runtime |
| playwright / yaml 被自动收集的模块数 | 52 / 15 | 复审独立复现为 **55 / 17**（机制结论不变：删与不删 hiddenimports 产物一致）|

**仍未修（诚实列出）**：AST 检查器仍可能被更巧妙的写法绕过——这正是把它定位为 lint 而非契约的原因。

---

## 13. 第五轮独立复审（对第四版）

对第四版的独立复审判定 **PASS**——**两条硬保障成立、产物版本资源正确**，判定标准是"硬保障 + 产物正确 → 只剩 lint/测试/文档层问题不阻塞收敛"。

### 它对两条硬保障的独立验证

| 命题 | 复审结论 | 证据 |
|---|---|---|
| 保障 1：厂商级错误让构建**崩**而非静默出空版本 exe | **成立**（在它声称的范围内）| `%TEMP%` 内最小 spec 真跑 PyInstaller：类名写错 → `NameError` exit 1 无 exe；模块改名 → `ModuleNotFoundError` exit 1；删赋值 → `NameError` exit 1。**边界**：把守卫改成恒假条件（`sys.platform == "linux"`）**不崩**、静默产出空版本，由 lint + 保障 2 兜住 |
| 保障 2：产物级断言能抓"版本资源没嵌入" | **成立** | 用 `UpdateResource(RT_VERSION)` 删掉真 exe 副本的版本资源后，真实断言失败：`产物 FileVersion=，期望 1.0.36.0`；`dist/` 缺失时为 `t.skip()`、`skipped` 计数如实 |
| 当前产物版本资源正确 | **成立** | 7/7 字符串字段 + 数字 `filevers/prodvers = 1.0.36.0` |

### 处置（本轮修复）

| 发现 | 内容 | 处置 |
|---|---|---|
| I-1 | 两条 `except` 变异的锚点用了**已不存在的** `os.name == "nt"` → 空操作，变异体是语法错误，测试靠 SyntaxError 回显关键词蒙混通过（等于不测任何东西）| ✅ 锚点改为 `sys.platform == "win32"`，关键词收紧为"存在 except" |
| I-2 | **方向反了**：检查器把 `set` 字面量当**合法**值接受，而 PyInstaller 会对 `hiddenimports` 调 `.extend()`，set/tuple 直接抛 `AttributeError` 崩构建 | ✅ set/tuple 改为**必须拒绝**（各加一条测试）；合法的 list 拼接改为"应接受" |
| I-3 | 检查器用了**未 import 的 `operator`** → 遇到合法的 list 拼接就 `NameError` 崩掉，错误信息还误导成"spec 解析失败" | ✅ 补 `import operator` |
| I-4 | §4.1 表的 v3 列与 §12 I2 自相矛盾（那 5 行只有本版才成立）；footnote ³ 把 a41f8a0 的反向启发式错记为 v2 | ✅ 表头改为"v4"并注明 v3 的实测结果；footnote 归属修正 |
| I-5 | **真实静默降级**：`backend:build` 用 `--noconfirm` 整体替换 COLLECT 目录，把 `copy:chromium` 拷入的 **267 MiB 浏览器内核删掉**；单独跑 `dist:win:app` 会静默产出缺内核的包 | ✅ 新增 `scripts/check-backend-artifact.mjs` + `npm run check:artifact`，接入 `dist:win`（在 `copy:chromium` 之后、`electron-builder` 之前）；`backend:build` 结束时主动警告。实测：当前 `dist/` 缺内核时该检查**正确失败**并给出补救命令 |
| M-1 | 探针只重放 import/顶层 If/顶层 Assign → `for`/`with` 里的 `_VERSION = None` 是盲区（lint 说没问题、产物真的没版本资源）| ✅ 收集范围扩到 `If/For/While/With/FunctionDef/ClassDef`；加 for 覆写测试 |
| M-2 | 合法写法被误拒：版本对象由顶层 helper 函数构造时探针 `NameError` 报问题，而该 spec 真跑 PyInstaller 是好的 | ✅ "执行求值失败"降级为**警告**（`findings.probe_warning`），只有 `ImportError`/`ModuleNotFoundError` 才是问题（那正是硬保障 1 会崩的情况）；加 helper 接受测试 |
| M-4/M-5 | 报告正文计数与描述过期（仍写 9/13/10 条、"恰好一次赋值"等已删的启发式）| ✅ 见下方"现行数字" |

### 现行数字（本节为准，覆盖前文任何旧值）

| 项 | 值 |
|---|---|
| `scripts/packaging-contract.test.mjs` | 10 tests |
| `scripts/spec-check.test.mjs` | **21** tests（1 真实 spec + 16 拒绝 + 4 接受）|
| `npm run test:packaging` | **31** |
| `npm run test:foundation` | **33** |
| `scripts/spec-check.mjs` 定位 | **lint**（不是契约；硬保障见 §4.1 末）|

### 复审指出但本轮**未修**的

- **M-3**：`packaging-contract.test.mjs` 中"已构建的后端产物…（含 filevers/prodvers 数字字段）"这条**标题对产物不成立**——它只读 4 个字符串字段；数字字段断言只作用于 spec 对象。硬保障未破，但标题夸大。
- **M-6**：NSIS 反向断言能拦住直连写法（`RMDir /r "$INSTDIR\data"`、大写 `RMDIR`、行尾注释、`Delete "$INSTDIR\Logs\*.*"`、删 `$INSTDIR` 本体），但拦不住间接写法（`StrCpy $0 "$INSTDIR\data"` + `RMDir /r "$0"`、`nsExec::Exec 'rmdir /s /q …'`）。仍是文本级而非语义级。
- **M-7**：`packaging-contract.test.mjs` 仍用 `execFileSync`（提取块失败会抛异常而非可读断言），且测试侧仍硬编码 `py -3.12`。
- **M-8**：探针无条件把 `sys.platform` 设为 `win32`，因此 spec 的 `else:` 分支从不被执行校验（只发 Windows，可接受，但已记入 §7）。
- **M-1 的剩余部分**：`with` 语句若依赖构建期上下文仍可能无法求值——已降级为警告，不再误报。
