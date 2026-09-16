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
#   5. hiddenimports 只列**运行时真实存在**的可选依赖。PyInstaller 对
#      hiddenimports 中缺失的模块会报错，因此 web 搜索（ddgs / duckduckgo_search）
#      这类"装了才生效"的可选后端**不能**写死在这里——它们由运行时
#      web_tools.py / browser_tools.py 自己 try-import，缺失时降级为可读提示。
#      打包机应先 `pip install ddgs` 再用本 spec，才能把该可选能力带进产物。

import os  # noqa: E402

# 版本资源仅 Windows 构建机可用。
#
# ⚠ 修复记录（阶段 0）：上游代码写的是 `FixedFileVersion`——PyInstaller 里
#   **不存在**这个类（正确名是 `FixedFileInfo`），于是 import 抛 ImportError，
#   又被 `except Exception: _VERSION = None` 静默吞掉，结果产物 exe 的
#   CompanyName/FileVersion 等全部为空。错误的类名 + 宽泛的 except 是这次
#   "静默失败"的两个原因，现分别修正为正确类名与只捕获 ImportError。
if os.name == "nt":
    # 导入失败即让构建失败：宁可不打包，也不产出没有版本资源的 exe。
    from PyInstaller.utils.win32.versioninfo import (  # noqa: F401
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
    # package.json 同源，滞后会让 exe 文件属性显示错误版本。
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
else:  # pragma: no cover - 非 Windows 构建机（v1 只发布 Windows x64）
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
