# StudyCrew Windows 构建与安装验证

项目基于 Knowe，保留根目录 MIT LICENSE 和上游归属。构建不需要模型 API Key，也不发送模型请求。下面从项目根目录执行，使用 PowerShell 7、Python 3.12 和项目提供的 Node 24 环境。

```powershell
. .\.tools\node24.ps1
py -3.12 -m venv .venv-build
.\.venv-build\Scripts\python.exe -m pip install -r backend/requirements-build.lock.txt
$env:STUDYCREW_DEV_PYTHON = "$PWD\.venv-build\Scripts\python.exe"
npm ci
```

`backend/requirements-build.lock.txt` 固定本次构建解析出的 Python 依赖版本。该文件包含构建和验证依赖，不包含凭据。本地 E5 模型不存入 Git，需要显式下载公开模型；下载器校验固定版本、文件大小和 SHA-256：

```powershell
.\.venv-build\Scripts\python.exe -m backend.knowe_memory.local_embedding --install
npm run typecheck
npm run lint
npm run test:foundation
npm run backend:build
```

冻结后端必须额外执行离线自检，检查实际打包的 NumPy、ONNX Runtime、E5、Mem0 和 USearch，包括中文语义召回。自检禁止网络；源码环境通过不能替代这一步。

```powershell
$checkReport = "$PWD\evals\runs\frozen-self-check.json"
$checkProcess = Start-Process -FilePath "$PWD\dist\StudyCrewBackend\StudyCrewBackend.exe" -ArgumentList @('--self-check', $checkReport) -WindowStyle Hidden -Wait -PassThru
$checkProcess.ExitCode
Get-Content -Encoding UTF8 $checkReport
npm run dist:win
```

安装产物为 `release/StudyCrew-Setup-1.0.36.exe`。交付前还需安装、启动、创建合成课程、退出、重启并确认恢复；构建成功本身不证明这些用户流程通过。升级验证应使用同一数据目录，保留已有状态。

本次使用项目内 `.installation-check` 安装目录。先在真实界面建好“安装恢复验证-合成”群组和“安装包离散数学”课程，再运行 `node scripts/installed_smoke.cjs`，验证升级恢复、实际计划、ICS 下载、本地语义记忆确认/召回/删除和重启。该脚本仅使用合成数据，不发送模型请求；输出保存在 `evals/runs/installed-final.json` 与截图中。

Office 图表视觉读取需要本机安装对应 Word、Excel 或 PowerPoint。转换器使用隐藏的只读 COM 实例生成 PDF，再渲染页面交给多模态模型；这不是文本解析的替代声明。应用已打开对应 Office 程序、包含宏或不支持的内容时会明确拒绝或报告失败。

安装包没有发行者签名证书，不执行发布和部署。模型配置、服务端密钥、评测数据目录中的 DPAPI 密钥和用户资料不应纳入源码或安装包。真实模型验证见 [评测说明](../evals/README.md)，严格使用共享预算账本。
