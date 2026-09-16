# 阶段 0：可复现的 Node 24 验证/构建环境
#
# 背景：本项目 engines 与 preflight 都要求 Node 24.x（Electron 43 内嵌 Node 24）。
#   宿主机的 Node 可能是 26+，那会让 preflight 失败，并让前端 vitest 因
#   Node 26 的 localStorage 变更而产生大量假性失败。本脚本下载官方 Node 24
#   发行版到 .tools/（已 gitignore），并把它前置到 PATH。
#
# 用法：. .\.tools\node24.ps1        （注意前面的点，在当前会话生效）
#      & .\.tools\node24.ps1 -Command 'npm run verify:all'
param(
  [string]$Version = '24.9.0',
  [string]$Command = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$dir = Join-Path $PSScriptRoot "node-v$Version-win-x64"
$nodeExe = Join-Path $dir 'node.exe'

if (-not (Test-Path $nodeExe)) {
  Write-Host "[node24] 未找到 Node $Version，正在下载…"
  $zip = Join-Path $PSScriptRoot "node-v$Version-win-x64.zip"
  $url = "https://nodejs.org/dist/v$Version/node-v$Version-win-x64.zip"
  Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
  Write-Host "[node24] 解压…"
  Expand-Archive -Path $zip -DestinationPath $PSScriptRoot -Force
  Remove-Item $zip -Force
}

$env:PATH = "$dir;$env:PATH"
Write-Host "[node24] node $( & $nodeExe --version )  根目录 $root"

if ($Command) {
  Push-Location $root
  try { Invoke-Expression $Command } finally { Pop-Location }
}
