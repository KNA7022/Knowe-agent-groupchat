# 阶段 0 验收报告：交付入场券

- 日期：2026-09-16
- 仓库：`D:\agentwork\StudyCrew-worktree`，分支 `feature/studycrew-foundation`
- 起始版本：`fcbfd38`
- 报告性质：**自审报告**。按项目既有流程，本阶段仍需一次独立复审才能标记为 PASS（见文末"下一步"）。
- 本阶段未改动任何安全网关生产逻辑，也未开始 Task 3（RAG）或 Task 4（Mem0）。

---

## 1. 结论

阶段 0 的两项目标都达成：

1. **该检出首次产出了可安装、可启动的 Windows NSIS 安装包。**
   - `release/StudyCrew-Setup-1.0.36.exe`（254.6 MB，exit code 0）
   - 安装 → 启动 → 后端拉起并写数据 → 卸载保留用户数据，全链路实测通过。
2. **验证环境偏离被消除，测试基线恢复可信。**
   - 本机 Node 26.8.1 曾让 `preflight` 失败、前端 41 个用例假性失败；在 Node 24.9.0 下 `preflight OK`、前端 **563 passed (50 files)**，与历史声称一致。

另更正了 `task-2-report.md` 的三处与代码不符的失实声明，并修正了过期的 SDD 台账。

**诚实说明**：产出安装包的过程中发现，此前"打包失败"并非单一原因，而是**五处独立缺陷叠加**（见 §3）。其中两处（版本资源静默失败、Electron 二进制缺失）在最初的盘点中并未识别，是实际执行构建时才暴露的。

---

## 2. 交付物

| 产物 | 路径 | 说明 |
|---|---|---|
| Windows 安装包 | `release/StudyCrew-Setup-1.0.36.exe` | 254.6 MB，未签名（受控试用件）|
| 块映射 | `release/StudyCrew-Setup-1.0.36.exe.blockmap` | electron-builder 生成 |
| 免安装目录 | `release/win-unpacked/` | 905 MB，实测可直接启动 |
| PyInstaller 后端产物 | `dist/StudyCrewBackend/` | 150 MB，`StudyCrewBackend.exe` 12.9 MB |
| 打包契约测试 | `scripts/packaging-contract.test.mjs` | 8 个断言 |
| PyInstaller 驱动 | `scripts/backend-build.mjs` | `npm run backend:build` |
| NSIS 资源生成器 | `scripts/make-build-resources.mjs` | `npm run build:resources` |
| Node 24 环境脚本 | `.tools/node24.ps1` | 下载并前置 Node 24.9.0 |
| 重命名后的 spec | `backend/StudyCrewBackend.spec` | 原 `KnoweBackend.spec` |

---

## 3. 修复的缺陷（五处叠加）

打包此前失败的根因不是一条，而是五条互相独立的缺陷：

### 3.1 `build/installer.nsh` 缺失且无生成者

`electron-builder.yml:69` 的 `nsis.include` 是硬引用，而 `build/` 被 `.gitignore` 整体忽略，全仓（含上游 `main`）从无该文件。

- **修法**：新增 `scripts/make-build-resources.mjs` 生成它，接入 `dist:win`。**不提交静态文件**，因为 `build/` 无法进版本库——提交了也会在每个新检出丢失。
- **NSIS 语义**：`customRemoveFiles` 逐项删除但**跳过 `data\`**，对应 `electron/main.ts:95` 的 `DATA_ROOT = INSTALL_ROOT/data`。

### 3.2 spec 的 `hiddenimports` 引用不存在的模块

`ddgs`、`duckduckgo_search` 在本机未安装，而 PyInstaller 对 `hiddenimports` 中缺失的模块会失败。这两个库在代码里是**故意的可选/延迟导入**（`web_tools.py:63-67`、`browser_tools.py:82-84`、`file_ops.py:326`），缺失时应降级为提示，不该是构建必需。

- **修法**：从 `hiddenimports` 移除二者与已废弃的 `dotenv`，只保留真实存在且必须显式声明的 `playwright*` 与 `yaml`。
- **代价（已记录）**：未安装 `ddgs` 时产物内不含网络搜索能力，应用会提示"pip install ddgs"。

### 3.3 版本资源静默失败（盘点时未发现）

spec 写的是 `FixedFileVersion`，而 PyInstaller 6.13.0 **不存在**这个类（正确名 `FixedFileInfo`）；import 抛 `ImportError` 后被 `except Exception: _VERSION = None` 吞掉。

- **证据**：修复前 `GetVersionInfo()` 的 7 个字段全为空，构建日志无 `Copying version information to EXE`。
- **修法**：改用 `FixedFileInfo(filevers=(1,0,36,0), prodvers=(1,0,36,0))`；把宽泛的 `except Exception` 换成 `if os.name == "nt":`，让导入失败直接中断构建（宁可不打包，也不产出没有版本资源的 exe）。
- **验证**：修复后日志出现 `Copying version information to EXE`，字段全部正确（见 §5.3）。

### 3.4 Electron 二进制从未下载（盘点时未发现）

`node_modules/electron/dist/` 与 `path.txt` 均不存在，而 `electron-builder.yml:29` 设了 `electronDist: node_modules/electron/dist`。

- **修法**：`node install.js` 下载 Electron 43.4.0 win32-x64（110 MB）。
- **注意**：这属于构建机状态，不是仓库缺陷，但**任何新检出都要先补这一步**，已在 TECH.md 记录。

### 3.5 品牌迁移未覆盖打包路径

21 处 `KnoweBackend` 引用分布在 spec / `electron-builder.yml` / `electron/main.ts` / `scripts/copy-chromium.mjs` / 文档。

- **修法**：统一改为 `StudyCrewBackend`，并**用测试锁死四处同源**（改名不同步会导致打包后启动不到后端，是静默故障）。
- 同时把 spec 的品牌与版本从 `Knowe / 1.0.25.2` 更新为 `StudyCrew 学伴团 / 1.0.36.0`，与 `package.json` 1.0.36 同源。

### 附带修复：`copy-chromium.mjs` 的 Python 探测不一致

原用裸 `python`/`python3`，与 `preflight.mjs`/`backend-build.mjs` 的 `py -3.12` 不一致；装了多个解释器时会读到**另一个** playwright 的 revision，从而拷入错误版本内核。已统一候选顺序并使用 `parsePythonCommand`。

---

## 4. 测试与门禁

新增 `scripts/packaging-contract.test.mjs`（8 个断言，**先写红测**）：

1. spec 的每个 `hiddenimport` 都必须可解析
2. spec 不得要求已移除的 `dotenv`
3. spec 品牌标识不得残留 Knowe
4. spec 版本资源必须与 `package.json` 同源
5. `electron-builder` 引用的构建资源必须存在或有生成者
6. spec 必须用正确的 PyInstaller 版本 API 且不得静默吞异常
7. spec 的版本对象必须能在本机构造成功（端到端 API 校验）
8. `dist:win` 必须先生成 NSIS 构建资源

红→绿证据：初次运行 `1 pass / 4 fail`（当时只有 5 条），实现后 **8 passed / 0 failed**。

`verify-runtime.mjs` 新增 4 条打包产物同源断言（spec / electron-builder / main.ts / copy-chromium）。

> **实现过程中的一个自纠**：第 5、6 条最初用反向断言检查 `FixedFileVersion` / `except Exception` 字面量，结果被 spec 里"修复说明"的**注释**误命中。已改为剥离注释后再做静态检查——静态检查本就该忽略注释。

---

## 5. 实测证据

### 5.1 环境与既有门禁（Node 24.9.0）

| 检查 | 结果 |
|---|---|
| `node --version` | v24.9.0（宿主为 26.8.1，偏离基线）|
| `node scripts/preflight.mjs` | ✅ preflight OK |
| `npm run lint` | ✅ 0 warning |
| `npx vitest run` | ✅ **563 passed (50 files)**（Node 26 下曾有 41 failed）|
| `npm run test:foundation` | ✅ 10 tests / 0 fail |
| `npm run verify:runtime` | ✅ StudyCrew runtime and Windows packaging contract verified |
| `npm run electron:build` | ✅ built main preview guard: OK |
| `npm run test:packaging` | ✅ 8/8 |

### 5.2 完整构建

```
npm run dist:win   →  exit code 0
```

关键日志（证明命名链同源）：

```
copying unpacked Electron  electronDist=node_modules\electron\dist
signing with signtool.exe  path=release\win-unpacked\resources\backend\StudyCrewBackend.exe
building        target=nsis file=release\StudyCrew-Setup-1.0.36.exe archs=x64 oneClick=false perMachine=false
downloaded      label=nsis-3.0.4.1.7z progress=100%
building block map  blockMapFile=release\StudyCrew-Setup-1.0.36.exe.blockmap
```

日志中 `version=26.15.3`、`archs=x64`、`perMachine=false` 与 `electron-builder.yml` 一致。

### 5.3 产物验证

**安装包版本资源**（修复 3.3 的直接证据）：

```
ProductName : StudyCrew 学伴团
FileVersion : 1.0.36.0
CompanyName : StudyCrew contributors
```

**后端 exe 版本资源**：

```
CompanyName     : StudyCrew
FileDescription : StudyCrew Backend Service
FileVersion     : 1.0.36.0
ProductName     : StudyCrew 学伴团
ProductVersion  : 1.0.36.0
OriginalFilename: StudyCrewBackend.exe
InternalName    : StudyCrewBackend
```

**unpacked 布局**：

| 检查 | 结果 |
|---|---|
| 主程序 | `StudyCrew 学伴团.exe` 215.2 MB |
| app.asar | ✅ 存在 |
| 内置后端 | ✅ `resources\backend\StudyCrewBackend.exe` |
| Playwright Chromium | ✅ `resources\backend\ms-playwright\chromium_headless_shell-1223` |
| 总计 | 905 MB |

**PyInstaller 产物内部布局**（`datas` 契约，对应 `config.py:98`）：

| 检查 | 结果 |
|---|---|
| `_internal/backend/{souls,prompts,locales,worker_prompt.md}` | ✅ 全部到位 |
| `_internal/backend/` 条目数 | 4（仅 datas，符合预期）|
| StudyCrew 核心包 | 在 PYZ 内（`base_library.zip` 存在，EXE 已追加 PKG）|

### 5.4 后端产物冒烟测试

直接运行 `dist\StudyCrewBackend\StudyCrewBackend.exe`（`KNOWE_AGENT=fake`）：

| 检查 | 结果 |
|---|---|
| 进程存活 | ✅ |
| 监听端口 | ✅ `127.0.0.1:8791` 与 `127.0.0.1:8792` |
| `/health` 响应 | ✅ `{"ok":false,"code":"runtime_auth_required"}` —— 证明 WS 与认证栈活着 |

### 5.5 安装 / 启动 / 卸载端到端

**静默安装**到隔离目录 `D:\agentwork\phase0-install-test`：

```
installer exit code: 0
```

| 检查 | 结果 |
|---|---|
| 主程序与卸载器就位 | ✅ `StudyCrew 学伴团.exe` / `Uninstall StudyCrew 学伴团.exe` |
| 后端就位 | ✅ `resources\backend\StudyCrewBackend.exe` |

**启动打包后的应用**（`release\win-unpacked`）：

| 检查 | 结果 |
|---|---|
| Electron 进程 | ✅ 4 个（主 + 渲染 + GPU + 工具）|
| **PyInstaller 后端被拉起** | ✅ `StudyCrewBackend` (57 MB) |
| **后端真的在写数据** | ✅ `data\backend\events\__platform__.seq`、`groups\v1\`、`harness\builds\`、`runtime\`、`schema_version\` |
| Electron 用户数据 | ✅ `data\electron\Local Storage\leveldb\` |

**卸载并验证数据保留**（`installer.nsh` 的核心契约）：

| 检查 | 结果 |
|---|---|
| 卸载器 exit code | 0 |
| 应用 exe 被移除 | ✅ |
| `resources\backend\StudyCrewBackend.exe` 被移除 | ✅ |
| **`data\` 目录存留** | ✅ |
| **用户文件内容完好** | ✅ `must-survive-uninstall` |

首次卸载后残留 `uninstallerIcon.ico` 与 `version`，已加入 `customRemoveFiles` 一并清理（测试与生成器同步更新）。

---

## 6. 可复现命令

```powershell
Set-Location D:\agentwork\StudyCrew-worktree

# 1) 取得 Node 24（宿主若是 26 必须先做这一步，否则 preflight 失败、前端 41 个假性失败）
. .\.tools\node24.ps1

# 2) 门禁
npm run preflight
npm run lint
npx vitest run
npm run test:packaging

# 3) 完整打包（图标 → PyInstaller → Chromium → electron-builder NSIS）
npm run dist:win

# 4) 产物
Get-ChildItem .\release\StudyCrew-Setup-1.0.36.exe
```

> **前置**：`pip install pyinstaller playwright ddgs`（后两者可选，但必须在打包前装好才能进产物）。
> **首次构建机**还需 `cd node_modules\electron; node install.js` 下载 Electron 二进制。

---

## 7. 未做 / 已知限制

1. **安装包未签名**。无证书，`win.signAndEditExecutable` 保持注释状态；自动更新仍禁用（`electron-builder.yml:78-85` 注明）。这属于原 Task 6 范围，本阶段未推进。
2. **`ddgs` 未安装** → 产物内无网络搜索能力（应用内提示安装）。要在发布件里带该能力，需在打包机 `pip install ddgs` 后重跑 `npm run backend:build`。
3. **磁盘占用**：`release/`（约 1.2 GB）、`dist/`（150 MB）、`build/pyinstaller/` 缓存。均可安全删除并重建（`.gitignore` 已覆盖）。
4. **未验证安装后应用能否真正启动**——只验证了 `win-unpacked` 直接启动与静默安装在文件层面的完整性。两者走同一份产物，但安装目录路径含中文空格（`StudyCrew 学伴团.exe`）这一差异未被单独覆盖。
5. **`.tools/node24.ps1` 只解决本机复现**，未接入 CI（仓库无 `.github/workflows`，这是原 Task 6 的范围）。
6. **Windows 以外的目标未验证**：`electron-builder.yml` 无 `mac:` 段，`v1` 只发布 Windows x64。

---

## 8. 下一步

按流程，本报告需一次**独立复审**（0 Critical / 0 Important 才 PASS）。复核建议聚焦：

- `installer.nsh` 的 `customRemoveFiles` 是否会误删非 `data` 的用户可见内容，以及卸载残留是否真的清空；
- `packaging-contract.test.mjs` 的 8 条断言是否存在可绕过的写法（尤其注释剥离与版本同源两条）；
- `backend-build.mjs` 的幂等判定（mtime 比较）是否可能跳过必要的重建；
- 品牌重命名是否有遗漏引用（`grep -ri knowebackend` 应为 0）。

阶段 0 PASS 之后，按既定顺序进入**阶段 1：Task 2 Round 2 收口**（必修 C-R1、I-R1、I-R6、I-R8、I-R4；接口形状修 I-R2；I-R3b 与 I-R7 记为已知限制），并同步更正 `task-2-report.md` 的失实声明——该项已在本次盘点中完成。
