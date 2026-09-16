# -*- mode: python ; coding: utf-8 -*-
# StudyCrewBackend.spec — PyInstaller 6.x 后端打包（v1.0.24.7 结构归一后，源码保密）
#
# 约定：本 spec 位于 <工作副本>/backend/ 下；PyInstaller 执行 spec 前会
#   chdir 到 spec 所在目录，因此 spec 内所有相对路径均相对本目录。
#
# 关键点（已审计定案）：
#   1. 入口 = run_backend.py（绝对导入 backend.server）；pathex=[SPECPATH]
#      保证 backend./knowe_provenance/knowe_core/knowe_harness/knowe_storage/
#      knowe_adapters 六个包可解析（v1.0.24.7 归一后 backend 为单层实现包）。
#   2. datas 目标不带双层前缀（归一前为 backend/backend）：引擎按 __file__
#      相对路径取数据（engine.py:_SOULS=parent/"souls"、zinnia.py:_PROMPTS_DIR=
#      parent.parent/"prompts"、i18n_backend.py:_DIR=parent/"locales"、
#      worker_gateway_runtime.py:Path(__file__).with_name("worker_prompt.md")），
#      打包后模块落在 _internal/backend/，故数据须同层布局。
#   3. console=False（窗口子系统），日志走 server.py 的文件双写保险丝。
#   4. 产物名 StudyCrewBackend 是**单一来源**：electron/main.ts 的 BACKEND_EXE、
#      electron-builder.yml 的 extraResources、scripts/copy-chromium.mjs 的
#      DIST_BACKEND 三处必须与 EXE/COLLECT 的 name 一致，否则打包后启动不到后端。
#      该一致性由 scripts/packaging-contract.test.mjs 与 scripts/verify-runtime.mjs 守卫。
#   5. hiddenimports 只列**必须显式声明**的延迟导入项。
#      PyInstaller 只打包构建时可导入的东西；playwright 与 yaml 的导入在字节码里
#      静态可见（browser_tools.py 的 `from playwright import async_api`、
#      file_ops.py 的 `import yaml`），其实会被自动收集——写在这里是"显式声明"，
#      不是它们进产物的唯一来源。web 搜索后端（ddgs / duckduckgo_search）是运行时
#      才决定的，缺失时由 web_tools.py 自己降级为可读提示，故不列入。
#      真正的前提是：**构建机必须装好这些包**（见 scripts/backend-build.mjs）。

import os  # noqa: E402
import sys  # noqa: E402

# ── 版本资源：让失败无法静默 ───────────────────────────────────────────────
#
# ⚠ 修复记录（阶段 0）：上游代码写的是 `FixedFileVersion`——PyInstaller 里
#   **不存在**这个类（正确名是 `FixedFileInfo`）。那次 import 抛 ImportError，
#   又被 `except Exception: _VERSION = None` 吞掉，于是产物 exe 的
#   CompanyName/FileVersion 等全部为空：构建成功、测试全绿、没人发现。
#
#   因此这里**故意不写任何 try/except**：类名写错、模块改名都会直接让
#   PyInstaller 崩掉，而不是产出一个没有版本信息的 exe。这是"失败无法静默"
#   这条原则的具体落实——静态检查（scripts/spec-check.mjs）只是补充 lint，
#   真正兜底的是"没有可吞的异常" + "产物级断言读真实 exe"。
#
#   非 Windows 构建机（v1 只发布 Windows x64）走 else 分支。
if sys.platform == "win32":
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VarFileInfo,
        VarStruct,
        VSVersionInfo,
    )

    # 版本单一来源 = package.json 的 version（三段）+ Windows 惯例第四段 build。
    # 每次发版必须同步：scripts/packaging-contract.test.mjs 会断言此处与
    # package.json 同源（含 filevers/prodvers 数字字段），滞后会让 exe 文件属性
    # 显示错误版本。
    _VERSION = VSVersionInfo(
        ffi=FixedFileInfo(
            filevers=(1, 0, 36, 0),
            prodvers=(1, 0, 36, 0),
        ),
        kids=[
            StringFileInfo(
                [
                    StringTable(
                        "040904B0",
                        [
                            StringStruct("CompanyName", "StudyCrew"),
                            StringStruct("FileDescription", "StudyCrew Backend Service"),
                            StringStruct("FileVersion", "1.0.36.0"),
                            StringStruct("InternalName", "StudyCrewBackend"),
                            StringStruct("OriginalFilename", "StudyCrewBackend.exe"),
                            StringStruct("ProductName", "StudyCrew 学伴团"),
                            StringStruct("ProductVersion", "1.0.36.0"),
                        ],
                    )
                ]
            ),
            VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
        ],
    )
else:  # pragma: no cover - 非 Windows 构建机
    _VERSION = None

# icon 在 <工作副本>/build/icon.ico（spec 上一级目录）。PyInstaller 把相对路径
# 按 spec 所在目录解析，故用 SPECPATH 推导而不是字面 'build/icon.ico'。
# build/ 被 .gitignore 忽略，由 scripts/make-icon-ico.mjs 或构建脚本生成。
_ICON = os.path.join(SPECPATH, "..", "build", "icon.ico")

a = Analysis(
    ["run_backend.py"],
    pathex=[SPECPATH],
    binaries=[],
    datas=[
        # (源相对 spec 目录, 目标相对 _internal/) —— 与 __file__ 相对路径匹配
        # v1.0.24.7 归一后 souls/prompts/locales 已在 backend/ 下，源相对 spec 目录直接写名字
        ("souls", "backend/souls"),
        ("prompts", "backend/prompts"),
        ("locales", "backend/locales"),
        ("worker_prompt.md", "backend"),
    ],
    hiddenimports=[
        # 仅列**必须显式声明且真实存在**的延迟导入项。
        # playwright 由 browser_tools._import_playwright() 延迟导入，需要显式声明；
        # yaml 由 file_ops.py 延迟导入（可选：装了才做更严格校验）。
        "playwright",
        "playwright.async_api",
        "playwright._impl._driver",
        "yaml",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["pytest", "tests", "IPython", "numpy", "scipy", "mkl", "mkl_fft", "mkl_random", "mkl-service"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="StudyCrewBackend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_ICON,
    version=_VERSION,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="StudyCrewBackend",
)
