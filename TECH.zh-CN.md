# Knowe — 技术概览

面向想运行、构建或扩展 Knowe 的开发者的快速技术指南。完整用户手册见 [docs 中文](./docs/CN/00-概述.md)（English: [docs/EN](./docs/EN/00-Overview.md)）。

---

## 架构一览

![Knowe 架构图](./docs/assets/TECH-architecture-zh.png)

- **前端**：Electron + React 18 + TypeScript，electron-vite 构建
- **后端**：Python 3.12（异步服务），PyInstaller 打包
- **通信**：本机 WebSocket（127.0.0.1:8080）+ 健康检查（127.0.0.1:8081）
- **数据**：全部本地存储——见下文「数据与隐私」

## Harness：任务生命周期与完成机制

Harness（`backend/knowe_harness/`，约 4300 行）是掌管每个任务完整生命周期的机制层——从任务信封与日志，经过完成状态机，到面向用户的投影与 outbox 投递。它保证机制上不可能出错：结果必须留痕、决策必须被记录——同时不限制 Agent 的行为。

![Harness 机制](./docs/assets/TECH-harness-zh.png)

## 仓库结构

| 路径 | 说明 |
|---|---|
| `electron/` | Electron 主进程与预加载（窗口、更新、托盘） |
| `src/` | React 渲染层（UI 组件、状态、i18n） |
| `backend/` | Python 后端（agents、harness、存储、知识库、工具） |
| `backend/tests/` | 后端测试（55+ 测试文件） |
| `docs/` | 完整用户手册（中英双语） |
| `assets/` | 宣传图与演示视频 |
| `scripts/` | 构建/开发辅助脚本 |

## 本地开发

环境要求：Node.js 24+、Python 3.12、模型 API Key（如 DeepSeek）。

```bash
# 安装前端依赖
npm install

# 安装后端依赖
pip install -r backend/requirements.txt

# 开发模式启动（前端 + Electron 外壳）
npm run electron:dev
```

开发模式下后端由 Electron 主进程自动拉起。仅需调试后端时，见 `backend/run_backend.py`。

### 校验 / 检查 / 测试

```bash
npm run typecheck   # TypeScript 类型检查（渲染层 + 主进程）
npm run lint        # ESLint
npm run test        # Vitest（前端）
# 后端测试：在 backend/ 下执行 pytest backend/tests/
```

### 配置

配置通过环境变量读取（见 `backend/config.py`）。常用项：

| 变量 | 作用 |
|---|---|
| `DEEPSEEK_API_KEY` | 模型服务 API Key（也可在应用内设置界面配置） |
| `DEEPSEEK_BASE_URL` | 自定义模型服务地址 |
| `KNOWE_AGENT` | Agent 模式：`fake`（离线，不调 LLM）或 `deepseek` |
| `KNOWE_DATA_DIR` | 覆盖数据目录 |

后端**不读** `.env` 文件。凭据不走环境变量、也不走明文文件：Provider Key 由 Electron
主进程经系统 `safeStorage` 保存，Python 侧只能兑换 purpose-bound 的短租。请在应用内
设置界面配置模型服务；上表中的非密钥变量（如 `KNOWE_AGENT`、`KNOWE_DATA_DIR`）如需
覆盖，直接设在环境里。

## 构建 Windows 安装包

完整构建管线：PyInstaller 打包后端 + electron-builder 产出 NSIS 安装包。

前置：Node.js 24.x（`npm run preflight` 会强制检查）、Python 3.12，并
`pip install pyinstaller playwright pyyaml ddgs`。PyInstaller 只打包构建时可导入的东西，
所以构建机上缺什么，产物里就缺什么：

- `pyinstaller` —— 必需。
- `playwright` —— 必需；缺了 `npm run backend:build` 会**直接失败**，而不是产出一个
  浏览器自动化能力静默缺失的应用。
- `pyyaml` 与 `ddgs` —— 可选；`backend:build` 只给警告。`pyyaml` 用于 `file_ops.py`
  里那个明确标注为可选的 YAML 严格校验，`ddgs` 提供网络搜索。想要这两项能力就装上。

全新检出还需要先下载一次 Electron 二进制（npm install 若跳过二进制下载则不存在）：

```bash
cd node_modules/electron && node install.js
```

`electron-builder.yml` 固定了 `electronDist: node_modules/electron/dist`，
该目录缺失时打包会明确报错。

```bash
# 一条命令全流程（NSIS 资源 → 图标 → PyInstaller → Chromium → electron-builder NSIS）：
npm run dist:win
```

或分步执行：

```bash
# 1. 生成 build/ 资源（build/ 被 .gitignore 忽略，二者每个检出都要重新生成）
npm run build:resources   # build/installer.nsh —— 被 electron-builder.yml 的 nsis.include 硬引用
npm run icon:ico          # build/icon.ico

# 2. 构建 Python 后端（PyInstaller）—— 在 backend/ 下执行
npm run backend:build

# 3. 准备浏览器自动化所需的 Chromium
npm run copy:chromium

# 4. 构建完整安装包（electron-vite build + electron-builder NSIS）
npm run dist:win:app
```

产物输出到 `release/`，命名 `StudyCrew-Setup-<版本号>.exe`。

> **注意**：修改后端代码后，**必须重新执行第 2 步（PyInstaller）**——electron-builder 只组装前端、不会重编后端。打包后请验证进包的 `StudyCrewBackend.exe` 时间戳是新生成的。

## 数据与隐私

- 所有数据**本地存储**于安装目录下 `data/`（项目、聊天记录、知识库、Token 账本）
- 聊天记录应用内永不删除
- Agent 在项目目录沙箱内活动，无法触碰项目根目录以外的文件
- 无遥测、无云端存储——一切留在你的机器上

## 常见问题

| 现象 | 可能原因 / 处理 |
|---|---|
| 应用能开但 Agent 不回应 | 模型 API Key 缺失或无效——设置 → 模型中配置 |
| 后端启动失败 | 8080/8081 端口被占用——检查是否有另一个 Knowe 实例在运行 |
| 安装包约 250MB | 正常：内置 Python 运行时 + 浏览器自动化 Chromium |

## License

[MIT](./LICENSE)
