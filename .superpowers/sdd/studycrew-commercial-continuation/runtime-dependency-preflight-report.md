# Shared Windows Python runtime dependency preflight — resolved and installed, with concerns

Date: 2026-10-04. Target: StudyCrew's approved Node 24 / CPython 3.12 / Windows x64 architecture. Scope here is the Python dependency and freezer intersection only; no Node/Electron execution or application integration was attempted.

**A single pinned wheel set resolves and installs successfully:** Mem0 OSS 2.0.20, ONNX Runtime 1.30.0, Tokenizers 0.23.2, the prior OCR image/geometry dependencies, real USearch 2.26.2, explicit Numkong 7.8.4 and the actual backend requirements. The protobuf conflict is resolved with **6.33.6**, without changing Mem0 or either model family. Real native USearch operations passed. **The combined native probe emits a generic shutdown diagnostic despite exit 0; clean combined-process shutdown remains an unresolved gate.** This report is not shipping, security, performance, model-quality or license-compliance certification.

Later bounded diagnosis in native-shutdown-diagnosis-report.md isolates this diagnostic to a test PyclipperOffset object retained until interpreter teardown. Explicitly releasing that object made both minimal and full guarded preflight variants pass with empty stderr, with original/corrected A/B/A/B evidence. Earlier raw results here remain unchanged. This establishes a corrected harness lifecycle, not the extension's internal cause, production ownership safety or frozen-app acceptance; an audit-hook interaction remains unexcluded.

## Boundaries and preserved evidence

Only this report was added to the worktree. Production source/tests/config/requirements/spec, Git index/HEAD, other agents' WIP and skills were not modified. No production lockfile, commit, full project suite, SDK/provider/model call, model weight acquisition, Qdrant service, Docker, global/project install or cleanup occurred. Backend Python Expert guidance informed the isolated interpreter, typed boundary awareness and explicit separation of successful checks from residual failures.

Owned external scratch: `D:/studycrew-runtime-dependency-preflight-20261004`. It contains acquisition/verification/probe scripts, seven newly acquired wheels, official release metadata, all selected wheel identities/metadata, offline resolver/install reports and logs, an isolated venv, extracted license/notice evidence and PE import inventory. Existing wheels were read from `D:/agentwork-e5-preflight-20261004/wheels`, `D:/agentwork-ocr-preflight-20261004/wheels` and `D:/agentwork/wheelprobe-mem0-full`; none of those environments or evidence files was changed. No existing model artifact was opened or executed.

CPython **3.12.10**, MSC v.1943, 64-bit AMD64; Windows **10.0.26300**; isolated pip **25.0.1**; `include-system-site-packages=false`. Venv was created from `E:/python/python.exe`. D: had **62,180,372,480 bytes** free before setup.

Read-only production inputs were `backend/requirements.txt` and `backend/StudyCrewBackend.spec`, after reading the five specified design/preflight reports. Input SHA-256 values at inspection: requirements `5a12861f6f62006367135141119d2a52c0783b55a1d482de5ddbbccd530bb4ad`; spec `a1462088c9668ef4ef46836b93aa34770740b2b5d72580b55ce6f5becc46442a`. No application source behavior was inferred from new WIP.

## Actual bounds and resolver outcomes

The following are **Requires-Dist entries read from the selected wheel ZIPs**, not guesses from package names or aggregate PyPI metadata:

| Intersection | Actual relevant wheel declarations | Selected result |
| --- | --- | --- |
| Protobuf | Mem0 `>=5.29.6,<7.0.0`; ORT `>=4.25.8`; Qdrant `>=3.20.0` | **6.33.6**. The common range is `>=5.29.6,<7`. |
| NumPy | ORT `>=1.21.6`; OpenCV `>=2` on Python >=3.9; Qdrant `>=1.26` on Python 3.12; USearch unbounded `numpy` | **2.5.3**. Qdrant's `<2.3.0` branch is for Python 3.10 and does not apply to 3.12. |
| HTTP clients | Backend `httpx>=0.27`; Mem0 `httpx>=0.28.0`; Qdrant `httpx[http2]>=0.20.0` | **httpx 0.28.1** plus h2/hpack/hyperframe. OpenAI 3.16.2 also requires **httpx2**; this separate namespace is retained. |
| Pydantic | Mem0 `>=2.7.3`; Qdrant `>=1.10.8,!=2.0.*,!=2.1.*,!=2.2.0` | **2.13.5**, matching prior SDK preflight, and exact **pydantic-core 2.46.5**. |
| Tokenizers | `huggingface-hub>=0.16.4,<2.0` | **1.33.0**, with its actual Hub/Xet closure; presence does not authorize Hub API use. |
| USearch | Windows 2.26.2 wheel says only `numpy`, `tqdm` | Explicit **numkong 7.8.4** is retained as an approved root, explained below. |
| Existing backend | `websockets>=14`, `httpx>=0.27`, `pypdfium2>=5.13`, `cryptography>=50`, `numpy>=2.0` | Added exact **websockets 17.2**, **pypdfium2 5.13.0**, **cryptography 50.0.2**, **cffi 2.1.1**, **pycparser 3.0**; all lower bounds satisfied. |

A deliberate offline pip dry-run for Mem0 2.0.20 + ORT 1.30.0 + protobuf 7.36.2 returned **exit 1 / ResolutionImpossible**, naming Mem0's upper bound. The shared roots with protobuf 6.33.6 returned **exit 0** and a **59-wheel** result. A second offline dry-run combined the exact hash lock with the **actual** backend requirements file: **exit 0**. Offline installation with `--require-hashes --only-binary=:all:` succeeded; `pip check` returned **No broken requirements found**.

These are resolver and local runtime results, not confirmation that newer baseline packages preserve every StudyCrew API call. In particular, websockets 17.2 satisfies the open lower bound but the actual server was not imported or tested. Build-only and independently installed tools such as PyInstaller/playwright are outside this 59-package set.

**USearch metadata discrepancy:** the official [2.26.2 release JSON](https://pypi.org/pypi/usearch/2.26.2/json) lists Numkong; the exact Windows wheel omits it from Requires-Dist. Its actual `usearch/__init__.py` nevertheless tries `from numkong import _numkong`, adds the native extension directory to DLL search and loads it with `ctypes.CDLL` on Windows. The real probe showed Numkong already imported before its explicit probe import. Keep the explicit root pin; do not depend on pip inferring it from aggregate release metadata.

The same USearch binary reports `USES_NUMKONG=0`, `USES_SIMSIMD=0`, `USES_OPENMP=0` and the tested index reports `hardware_acceleration="serial"`. Loading the Numkong extension does **not** establish that USearch uses its accelerated kernels. The PE import table also contains no Numkong DLL dependency for this compiled USearch wheel. This is actual USearch native indexing with serial kernels, not NumPy pretending to be USearch.

## Acquisition and byte accounting

**New wheel bodies: 8,997,641 bytes** across seven wheels (USearch, Numkong, websockets, cryptography, pypdfium2, cffi and pycparser). **Reused selected wheel bodies: 111,702,456 bytes**. Selected compressed-wheel total: **120,700,097 bytes**. Sum of uncompressed members of the 59 wheel archives: **336,193,785 bytes**. None of these is frozen backend size, installer delta, runtime memory or complete application disk usage.

The acquisition script counted **18,085,082 new response-body bytes**, including the seven official PyPI metadata responses and wheel bodies, under the **150,000,000-byte cap**. No retry or partial-download failure occurred. This is application-level body accounting, excluding HTTP/TLS overhead and the two browser metadata views. Only 8,997,641 of those counted bytes are dependencies; no weights were acquired. Scratch pip commands were `--no-index --no-cache-dir`.

New wheel size/hash matched its exact official `files.pythonhosted.org` record before installation. Reused wheels were rehashed against the retained, previously independently verified E5/OCR hash locks and Mem0 `wheel-verification.json`. Every selected hash was additionally compared with pip's resolver artifact hash. This does not claim a fresh independent publisher lookup for every cached artifact.

Official new package records: [USearch](https://pypi.org/pypi/usearch/2.26.2/json), [Numkong](https://pypi.org/pypi/numkong/7.8.4/json), [websockets](https://pypi.org/pypi/websockets/17.2/json), [cryptography](https://pypi.org/pypi/cryptography/50.0.2/json), [pypdfium2](https://pypi.org/pypi/pypdfium2/5.13.0/json), [cffi](https://pypi.org/pypi/cffi/2.1.1/json), [pycparser](https://pypi.org/pypi/pycparser/3.0/json). Exact immutable wheel URLs remain in `inventory.json`.

## Real import/native evidence and its limits

In one isolated process with the installed shared closure:

- **USearch:** actual `usearch/compiled.cp312-win_amd64.pyd`; `Index(ndim=384, metric="cos", dtype="f32", multi=False)`; add three explicitly synthetic vectors at keys 11/22/33, search with one thread and three matches. Identical/orthogonal/opposite vectors returned keys **11,22,33**, distances **0,1,2**; `1-distance` yields **1,0,-1** for the existing higher-is-more-similar convention. Remove 11, confirm absence, re-add, confirm presence, reset, confirm size zero all passed. No save/load/view, persistent index or exact NumPy scanning was used. Three simple vectors prove mechanics, not HNSW recall, concurrency, replacement policy, performance or semantic quality.
- **ORT 1.30.0:** native import and provider enumeration passed. Available providers were **AzureExecutionProvider and CPUExecutionProvider**, even in the CPU wheel. No session/model call occurred; production must explicitly request CPUExecutionProvider and disable fallback, rather than interpret package naming as an allowlist.
- **Tokenizers:** Rust extension loaded and encoded one synthetic WordLevel token. No E5 tokenizer or model was opened. Prior real E5/OCR inference evidence was obtained with protobuf 7.36.2; it was **not rerun under this unified 6.33.6 closure**.
- **OCR dependencies:** OpenCV resize, Shapely/GEOS polygon area, Pyclipper polygon offset and Pillow image allocation passed. These are native dependency checks, not OCR preprocessing/parity or recognition acceptance.
- **Mem0:** real `mem0ai==2.0.20` import and `Memory.__module__=="mem0.memory.main"`; protobuf Struct serialize/parse and Qdrant's generated `points_pb2` import passed. No Memory instance, SDK operation, provider, Qdrant backend or PostHog client was constructed. This does not repeat the separate SDK orchestration tests.
- **Existing baseline:** AES-GCM encrypt/decrypt with a synthetic constant fixture, PDFium new in-memory document/close, and websockets/SQLAlchemy/PyYAML/win32api imports passed. No application DB, user file or server was opened.

Before imports, the probe set private MEM0_DIR, telemetry/offline flags and a Python audit hook denying socket connect/DNS/send and process creation. A deliberate DNS audit self-test was denied **before resolution**; the recorded event list contained only that self-test. The constructor profile guard observed zero Qdrant/PostHog constructor attempts, and only MainThread remained at the recorded checkpoint. The owned Mem0 directory contained only `config.json`. This is Python-level instrumentation and a checkpoint, not native socket interdiction, firewall/packet-capture evidence, filesystem forensic evidence or a proof covering interpreter teardown.

**Unresolved combined shutdown:** the assertions completed and subprocess exited 0, but stderr printed:

```text
Error in sys.excepthook:

Original exception was:
```

The first structured result is preserved as `native-results-first.json`; `native.log` and `commands.json` retain the diagnostic. Six separate guarded import processes (Numkong, USearch, ORT, OCR libraries, Mem0, and baseline libraries) each exited 0 with empty stderr. A targeted combined rerun with the constructor profiler disabled still emitted the diagnostic; its assertions are retained in `native-results-no-profile.json` and the tool transcript. Thus the profiler is not established as its cause. The diagnostic cannot be classified as harmless or attributed to a particular package from this bounded evidence. No package/source was patched, exit was not forced, and the error was not suppressed. **Do not call combined startup/shutdown clean until it is explained and revalidated.**

## Binary/platform closure and concrete freezer boundaries

Every selected wheel has a CPython 3.12-compatible Windows x64 or universal tag; no source build was used. All **118 DLL/PYD members** parsed as PE AMD64 (`Machine=0x8664`); `pe-inventory.json` records normal/delay imports. This checks these binary member headers, not every optional executable, CPU instruction path, static library, vulnerability or dynamic-load target.

The actual spec sets `binaries=[]`, `hookspath=[]`, `runtime_hooks=[]`, lists only playwright/yaml hidden imports and **explicitly excludes numpy**. It contains no explicit Mem0/ORT/Tokenizers/USearch collection or bootstrap boundary. Existing automatic PyInstaller hooks may collect some items after imports are wired; this inspection does not claim every file below is absent from a build that was never run. The following boundaries must be made concrete and checked in Analysis/COLLECT and the executable:

| Boundary | Actual wheel evidence and inclusion requirement |
| --- | --- |
| NumPy | Remove the conflicting exclusion; preserve needed core/linalg extensions and `numpy.libs/libscipy_openblas64_-ed4f167a5330424524f45258e7ca2c8d.dll`, its hashed MSVCP140 companion and DLL loader ordering. Do not collect test extensions solely because this inventory lists them. |
| ORT | Preserve `onnxruntime/capi/onnxruntime_pybind11_state.pyd`, `onnxruntime.dll`, `onnxruntime_providers_shared.dll` and package layout/metadata. Actual PE imports include MSVCP140, MSVCP140_1, VCRUNTIME140, VCRUNTIME140_1, UCRT/API sets and delayed SHELL32. An installed VC runtime on this host is not a clean-machine redistributable prerequisite test. |
| USearch + Numkong | Collect `usearch.compiled` and `numkong._numkong` with exact package-relative paths. Preserve `__file__`-based ctypes loading and DLL-directory setup in the frozen process. Both use CPython312/VC runtime; USearch also uses MSVCP140/VCRUNTIME140_1. Do not call `usearch.sqlite_path` / BinaryManager: that separate API contains a GitHub binary download and is outside the in-memory index plan. |
| Tokenizers / Hub | Preserve `tokenizers/tokenizers.pyd`; separately account for `hf_xet/hf_xet.pyd` if collecting the full declared closure. No Hub/model auto-download API is needed. Preserve distribution metadata used for version verification and explicit offline bootstrap. |
| OpenCV / geometry | Preserve `cv2.pyd`, cv2 Python loader/config data and any chosen video DLL policy. Wheel includes `opencv_videoio_ffmpeg500_64.dll`. Shapely requires its three PYDs plus hashed GEOS/GEOS-C/MSVCP140 DLLs in `shapely.libs`; collecting `shapely` Python files alone loses its sibling DLLs. Collect `pyclipper._pyclipper` and required Pillow extensions. |
| Mem0 and transitive native state | Explicitly include actual application adapter factory import paths when implemented, Mem0 configs/factories/data and dist-info; avoid collecting every optional vector/NLP provider. Retain Qdrant imported types/generated protobuf, `google._upb._message`, `grpc._cython.cygrpc`, pydantic-core, jiter, greenlet, SQLAlchemy extensions, and portalocker's Windows/pywin32 path as used. Base Qdrant import is not Qdrant backend authorization. |
| Existing native baseline | Preserve `cryptography/hazmat/bindings/_rust.pyd`, `_cffi_backend`, `pypdfium2_raw/pdfium.dll` and its ctypes/resource paths. PDFium's `py3-none-win_amd64` tag does **not** mean the wheel is native-free. |
| Early bootstrap / exclusions | Telemetry/offline flags and private MEM0_DIR must precede any transitive Mem0 import. Current spec has no runtime hook and no spaCy exclusion. Use a locked build interpreter and verify absence of ambient spaCy/NLP models/Paddle/Torch/Transformers or fail closed; do not let optional static imports pull in model-download branches. |

CPython's `python312.dll` and `python3.dll`, the Windows VC/UCRT dependencies and OS API set support are platform prerequisites beyond wheel resolution. Native imports succeeded on this machine only. No PyInstaller build, hook compatibility test, clean Windows VM, installer size measurement, Node 24/Electron sidecar launch or package-license output was produced. The production lock and build fingerprint remain future implementation work by their owner.

## License and redistribution evidence

The table below records declared wheel-level licenses, not a legal conclusion about a frozen distribution. Complete available license/notice members were copied byte-for-byte to owned `notices/<package>/...`; original wheel evidence remains read-only.

- **GEOS:** Shapely's actual `LICENSE_GEOS` names bundled GEOS and **LGPLv2.1**; hashed GEOS and GEOS-C DLLs are present. Preserve the license and resolve applicable redistribution/source/relinking provisions for the shipped DLL arrangement.
- **OpenCV/FFmpeg:** actual wheel includes FFmpeg DLL and `cv2/LICENSE-3RD-PARTY.txt`, which identifies FFmpeg redistribution and includes LGPL texts. Do not label the entire image stack Apache-only or treat headless as absence of FFmpeg. The broad notice file also covers other platforms; inventory actual binaries rather than assigning every listed component to Windows.
- **NumPy:** bundled notices name OpenBLAS (BSD-3-Clause), LAPACK and **GCC runtime GPL-3.0-or-later WITH GCC-exception-3.1**, in addition to NumPy's project expression. Preserve applicable bundled notices and exception text; do not reduce this to the project BSD label.
- **ORT:** `onnxruntime/LICENSE` and `ThirdPartyNotices.txt` are available; retain both. USearch and Numkong ship Apache-2.0 license text.
- **Tokenizers gap:** the exact wheel declares an Apache classifier but contains **no license/notice member**. The prior design identifies Apache-2.0 upstream. A version-corresponding upstream license/third-party notice inventory must still be sourced before release; an SPDX-like label alone is not a redistributable notice file.
- **Pillow, PDFium, cryptography and CFFI:** retain the actual bundled materials. PDFium supplies Windows build notices for PDFium, Abseil, AGG, FreeType, ICU, codecs and others under `dist-info/licenses/data/windows_x64/BUILD_LICENSES`. Cryptography's wheel has Apache/BSD texts; its native/static cryptographic dependency notice completeness was not established by this file-name inventory.
- **MPL components:** certifi declares MPL-2.0 and tqdm declares MPL-2.0 AND MIT. All copyright, notice, license and any applicable source obligations must be carried into a release inventory. Model pack licenses are separate and were not downloaded or completed here.

## Exact selected pins, hashes and wheel licenses

One selected wheel per package. Tags below are the exact filename tag suffix; original full names, Requires-Python/Requires-Dist, native members and notice paths are in `selected.json`. The external `requirements.lock` is scratch reproducibility evidence only, not a production lock.

| Package | Pin | Wheel tag | Bytes | Declared license / evidence note | SHA-256 |
| --- | --- | --- | ---: | --- | --- |
| annotated-types | 0.8.0 | py3-none-any | 13,427 | MIT | `f072f4d804ea359e4eaf198b1af7a8b0943881a87f31bb764f8bf219bb9419e0` |
| anyio | 4.15.1 | py3-none-any | 132,079 | MIT | `6152fdbbf9a77fdec97731721bebf7c4c44f7c29b424b0065826173efc7ed101` |
| backoff | 2.2.1 | py3-none-any | 15,148 | MIT | `63579f9a0628e06278f7e47b7d7d5b6ce20dc65c5e96a6f3ca99a6adca0396e8` |
| certifi | 2026.7.22 | py3-none-any | 136,983 | MPL-2.0 | `62f22742b58a1a33014a2b6b706588a8d7e2a88ae7bd1a6ebe8c992928483775` |
| cffi | 2.1.1 | cp312-cp312-win_amd64 | 185,919 | MIT-0 | `f53e442b08449d42821fa4a4fba000095af9f62742a500f978a9f557ec44339a` |
| charset-normalizer | 3.5.1 | cp312-cp312-win_amd64 | 200,551 | MIT | `3617ac3cfd8b9888f145ad89dd6e692285834b0201c6074a5eeaad3fd4d668c2` |
| click | 8.5.0 | py3-none-any | 125,251 | BSD-3-Clause | `255bc9599cf7748b4b1a446ccc735421bd08a2ae529a8b88597d3de5664ee360` |
| colorama | 0.4.6 | py2.py3-none-any | 25,335 | BSD-3-Clause (bundled text) | `4f1d9991f5acc0ca119f9d443620b77f9d6b33703e51011c16baf57afb285fc6` |
| cryptography | 50.0.2 | cp311-abi3-win_amd64 | 3,824,692 | Apache-2.0 OR BSD-3-Clause | `7afa5a6602a9f29af1f3a2965f831bae7c9d5d597b7cbb716d41ab3b7d89879c` |
| distro | 1.9.0 | py3-none-any | 20,277 | Apache License, Version 2.0 | `7bffd925d65168f85027d8da9af6bddab658135b840670a223589bc0c8ef02b2` |
| filelock | 4.0.10 | py3-none-any | 111,193 | MIT | `698048d158dc3c9e18cc5e88b3efdd4bd0e77d76686b22adb4511cc4b2c2f0db` |
| flatbuffers | 25.12.19 | py2.py3-none-any | 26,661 | Apache 2.0 | `7634f50c427838bb021c2d66a3d1168e9d199b0607e6329399f04846d42e20b4` |
| fsspec | 2026.9.0 | py3-none-any | 221,738 | BSD-3-Clause | `8dd6e646e99ea382bd85f97a45e6b526a442d79423a7dc673f1e2756d05fcb5f` |
| greenlet | 3.5.6 | cp312-cp312-win_amd64 | 324,203 | MIT AND PSF-2.0 | `7ac4abb3877c43af320392c664774eef6fa2cc063c79a55fc02d844a3cbe7395` |
| grpcio | 1.84.0 | cp312-cp312-win_amd64 | 5,253,534 | Apache-2.0 | `49717e857899f4136d7657bf5aded61ac479110a075438290923a4d86af7cd02` |
| h11 | 0.16.0 | py3-none-any | 37,515 | MIT | `63cf8bbe7522de3bf65932fda1d9c2772064ffb3dae62d55932da54b31cb6c86` |
| h2 | 4.4.1 | py3-none-any | 62,636 | MIT | `0e25f1462b23c9cb82d9eb02e28bc706dac2a68cb457c6a0d74d63c8a2a5d0e6` |
| hf-xet | 1.6.0 | cp38-abi3-win_amd64 | 4,033,128 | Apache-2.0 | `fb4fadde1b2b70bf4c0c14a6dccbe7194b1c28947fefd5bbe3fed9d940676c3b` |
| hpack | 4.2.0 | py3-none-any | 34,246 | MIT | `858ac0b02280fa582b5080d68db0899c62a80375e0e5413a74970c5e518b6986` |
| httpcore | 1.0.9 | py3-none-any | 78,784 | BSD-3-Clause | `2d400746a40668fc9dec9810239072b40b4484b640a8c38fd654a024c7a1bf55` |
| httpcore2 | 2.13.0 | py3-none-any | 83,162 | BSD-3-Clause | `35ae5be347aa40467b4a5dc032ac67ebb6d27189fc97e8cebcf99616f6a1bb9e` |
| httpx | 0.28.1 | py3-none-any | 73,517 | BSD-3-Clause | `d909fcccc110f8c7faf814ca82a9a4d816bc5a6dbfea25d6591d6985b8ba59ad` |
| httpx2 | 2.13.0 | py3-none-any | 95,565 | BSD-3-Clause | `fc12720cedf72faa26cca6b4ca394e05c894e7d7933fc45cafe767960804e49a` |
| huggingface_hub | 1.33.0 | py3-none-any | 846,435 | Apache-2.0 | `04e434b06e100eddbce9a6e817d72693a7884b10a79bd67ab48080d5c07eb899` |
| hyperframe | 6.1.0 | py3-none-any | 13,007 | MIT | `b03380493a519fce58ea5af42e4a42317bf9bd425596f7a0835ffce80f1a42e5` |
| idna | 3.20 | py3-none-any | 69,583 | BSD-3-Clause | `ab7ae7122974553370f0bdb919e1a960b2cd1bc1ef0276416d896db81c14582c` |
| jiter | 0.17.0 | cp312-cp312-win_amd64 | 227,381 | MIT | `9bd3caac219df476dd0cc3fe01d2f1581ed588906feac767abd9614c1c12f8b3` |
| mem0ai | 2.0.20 | py3-none-any | 345,797 | Apache-2.0 | `655103616fe70b67e0434f6725aad35269a071adcd1f61426553b10e0b1b076c` |
| numkong | 7.8.4 | cp312-cp312-win_amd64 | 495,364 | Apache-2.0 | `1d60472053cf31bb0726e5ce95ba9267c7f221ea253983c400367887300070f3` |
| numpy | 2.5.3 | cp312-cp312-win_amd64 | 12,567,828 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | `0a59a421a32580a009e8a1751345bf829631b990dc1794b80514ab722b435def` |
| onnxruntime | 1.30.0 | cp312-cp312-win_amd64 | 14,311,470 | MIT License | `f3501472571f1b1eee50e017851e7929f5ea37312d2d8c2494a19e8fc58b4a38` |
| openai | 3.16.2 | py3-none-any | 2,053,568 | Apache-2.0 | `660a7f8307e6605342ae84ff4425412e7cd19e5e9b298f975502df2a1a9a5d5f` |
| opencv-python-headless | 5.0.0.93 | cp37-abi3-win_amd64 | 43,825,962 | Apache 2.0 | `829717b6a95554f273e49e357cee3b3a2a26b6f4842fbc1bed2b45bdd8f87e0e` |
| packaging | 26.3 | py3-none-any | 129,956 | Apache-2.0 OR BSD-2-Clause | `d7193f7c8e4e93f444fde0262bf90af30e16fa0ad0ad44cb553c87339b23cd1c` |
| pillow | 12.3.0 | cp312-cp312-win_amd64 | 7,227,137 | MIT-CMU | `a2b55dd6b2a4c4b7d87ffa56bdb33fdc5fdb9a462173861a7bc097f17d91cb09` |
| portalocker | 3.2.0 | py3-none-any | 22,424 | BSD-3-Clause | `3cdc5f565312224bc570c49337bd21428bba0ef363bbcf58b9ef4a9f11779968` |
| posthog | 7.58.0 | py3-none-any | 781,569 | MIT | `2ac581ce52c63acb3a551c9066652c3d29e1de1ea06e70655ebe8b866e80f334` |
| protobuf | 6.33.6 | cp310-abi3-win_amd64 | 437,089 | 3-Clause BSD License | `0cd27b587afca21b7cfa59a74dcbd48a50f0a6400cfb59391340ad729d91d326` |
| pyclipper | 1.4.0 | cp312-cp312-win_amd64 | 104,619 | MIT | `d1e5498d883b706a4ce636247f0d830c6eb34a25b843a1b78e2c969754ca9037` |
| pycparser | 3.0 | py3-none-any | 48,172 | BSD-3-Clause | `b727414169a36b7d524c1c3e31839a521725078d7b2ff038656844266160a992` |
| pydantic | 2.13.5 | py3-none-any | 472,589 | MIT | `346a034f080da3755d8e9cb5e00e8b07de1d39e4f6e2c87d8ab7cafa0b269a73` |
| pydantic_core | 2.46.5 | cp312-cp312-win_amd64 | 2,043,140 | MIT | `5e81740c09e310f5aa5cbd3e434a01c154d4bef93241c7877b39f211d2b78ba8` |
| pypdfium2 | 5.13.0 | py3-none-win_amd64 | 3,885,553 | BSD-3-Clause, Apache-2.0, dependency licenses | `47dcca2a8d507b5fd24f94c3c9d48fb379430f097bc20f01beff6c963ffbcedb` |
| pytz | 2026.3.post1 | py2.py3-none-any | 508,283 | MIT | `dd95840dd199baea12d9cc096a1d452caa6596a1c1e4b5f3dbd1541855d5e815` |
| pywin32 | 312 | cp312-cp312-win_amd64 | 6,914,841 | PSF | `b457f6d628a47e8a7346ce22acb7e1a46a4a78b52e1d17e1af56871bd19a93bc` |
| PyYAML | 6.0.3 | cp312-cp312-win_amd64 | 154,003 | MIT | `5fcd34e47f6e0b794d17de1b4ff496c00986e1c83f7ab2fb8fcfe9616ff7477b` |
| qdrant-client | 1.19.1 | py3-none-any | 406,533 | Apache-2.0 | `fca1a96c3f90f5fff853f6ee6877838a5768a04c963df9891a655a63313af8a0` |
| requests | 2.34.2 | py3-none-any | 73,075 | Apache-2.0 | `2a0d60c172f83ac6ab31e4554906c0f3b3588d37b5cb939b1c061f4907e278e0` |
| shapely | 2.1.2 | cp312-cp312-win_amd64 | 1,722,861 | BSD 3-Clause | `743044b4cfb34f9a67205cee9279feaf60ba7d02e69febc2afc609047cb49179` |
| sniffio | 1.3.1 | py3-none-any | 10,235 | MIT OR Apache-2.0 | `2f6da418d1f1e0fddd844478f41680e794e6051915791a034ff65e5f100525a2` |
| SQLAlchemy | 2.0.54 | cp312-cp312-win_amd64 | 2,170,005 | MIT | `d65f8ca742ef1e1e14bc417ef59dc2ddf207a7b66b30cfdc6152447314e030cf` |
| tokenizers | 0.23.2 | cp310-abi3-win_amd64 | 2,863,236 | Apache family classifier; text absent from wheel | `2e96f5699d5249c9c64aa8412e044f727aae3a4098cf830f9901ec1afc361cde` |
| tqdm | 4.70.1 | py3-none-any | 80,199 | MPL-2.0 AND MIT | `c293e525e6fef9c20e8728fd4612df02a0aa31bb5fe91ecd93e123b1b7bffa73` |
| truststore | 0.10.4 | py3-none-any | 18,660 | MIT | `adaeaecf1cbb5f4de3b1959b42d41f6fab57b2b1666adb59e89cb0b53361d981` |
| typing-inspection | 0.4.4 | py3-none-any | 14,750 | MIT | `65b8397ba37ccbce054456aaccddfc91e6e3083c92824df348d96ca832f3f147` |
| typing_extensions | 4.16.0 | py3-none-any | 45,571 | PSF-2.0 | `481caa481374e813c1b176ada14e97f1f67a4539ce9cfeb3f350d78d6370c2e8` |
| urllib3 | 2.8.0 | py3-none-any | 135,717 | MIT | `0cf3cae568d36aa9576b28dfb35f11328f1cb974ca7647d9475ebb86c75ac6e3` |
| usearch | 2.26.2 | cp312-cp312-win_amd64 | 346,058 | Apache-2.0 | `7bf14812781cac3e4cb304f09ca924726def8070608100597b09433e7487ea47` |
| websockets | 17.2 | py3-none-any | 211,883 | BSD-3-Clause | `6aa59f0ef92e796b2db6f5f26550c4713c0e4036899fadf02f55e2ed4db0b7ae` |

## Reproduction and evidence files

Run from the owned scratch with PowerShell 7 and UTF-8. The scripts write named scratch evidence; preserve existing outputs before any later rerun.

```powershell
& 'E:/python/python.exe' -m venv 'D:/studycrew-runtime-dependency-preflight-20261004/venv'
& 'D:/agentwork-e5-preflight-20261004/venv/Scripts/python.exe' -B -X utf8 'D:/studycrew-runtime-dependency-preflight-20261004/acquire.py'
& 'D:/studycrew-runtime-dependency-preflight-20261004/venv/Scripts/python.exe' -B -X utf8 'D:/studycrew-runtime-dependency-preflight-20261004/verify_install.py'
& 'D:/studycrew-runtime-dependency-preflight-20261004/venv/Scripts/python.exe' -B -X utf8 'D:/studycrew-runtime-dependency-preflight-20261004/checks.py'
& 'D:/studycrew-runtime-dependency-preflight-20261004/venv/Scripts/python.exe' -B -X utf8 'D:/studycrew-runtime-dependency-preflight-20261004/pe_inventory.py'
& 'D:/studycrew-runtime-dependency-preflight-20261004/venv/Scripts/python.exe' -B -X utf8 'D:/studycrew-runtime-dependency-preflight-20261004/shutdown_diagnostic.py'
```

The initial resolver command (required before verify_install.py) was:

```powershell
& 'D:/studycrew-runtime-dependency-preflight-20261004/venv/Scripts/python.exe' -m pip install --isolated --disable-pip-version-check --no-index --no-cache-dir --only-binary=:all: --ignore-installed --dry-run --find-links 'D:/agentwork-e5-preflight-20261004/wheels' --find-links 'D:/agentwork-ocr-preflight-20261004/wheels' --find-links 'D:/agentwork/wheelprobe-mem0-full' --find-links 'D:/studycrew-runtime-dependency-preflight-20261004/wheels' -r 'D:/studycrew-runtime-dependency-preflight-20261004/roots.txt' --report 'D:/studycrew-runtime-dependency-preflight-20261004/resolver.json'
```

`verify_install.py` verifies cached/new hashes, constructs the external lock and installs with the same four find-links directories plus `--require-hashes`. `commands.json` preserves exact conflict/baseline-intersection/pip-check/native commands, exit codes and stderr. `resolver.json`, `baseline-resolver.json`, `install.json`, `install.log`, `conflict.log`, `baseline_intersection.log`, `pip_check.log`, `native.log`, `native-results*.json`, `shutdown-diagnostic.json`, `inventory.json` and `pe-inventory.json` separate each evidence level.

One verifier harness correction occurred before installation: the Mem0 verification file is an object containing a `wheels` list, not a root list; the initial indexing failure installed nothing. Later constructor-profiling shutdown cleanup and audit-hook retained references did not remove the generic combined shutdown diagnostic. These are scratch harness edits, not SDK/package modifications.

Preserved evidence hashes: `selected.json` **68bf0fe39e2cbaba9747d7d2852d2c8b6aeabc3a0d4b36439c808f3213fbc57c**; external `requirements.lock` **9ea7a68a5da8d179fb92c45fc5199d747b3cbd3a76c36a13e075d80dc784fe4a**; `native-results.json` **6ea84f8db1afbaecdb2387e8250af581cf75d34f48c7b7d7e4e3696b90ba93ac**; `pe-inventory.json` **cc6a3ad1108a09b08a5ae9b349452851ead7b67b85344308c48b4594085f4af2**.

The bounded conclusion is a compatible, reproducible **candidate dependency set** with actual install and native-index evidence. Combined clean shutdown, same-closure E5/OCR inference, real application/SDK adapters, freeze hooks/early bootstrap, native no-egress, supported CPU/Windows behavior, notice completeness, vulnerability review and full packaged acceptance remain distinct gates. None permits changing the approved model/Mem0 requirements or falling back to cloud.
