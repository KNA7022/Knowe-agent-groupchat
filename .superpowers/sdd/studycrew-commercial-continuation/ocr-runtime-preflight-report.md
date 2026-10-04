# PP-OCRv6 Small real-runtime preflight — passed with concerns

Date: 2026-10-04. This is an isolated Windows CPU artifact/runtime experiment and preparation for adapter parity work, not production implementation, packaging approval, quality acceptance or pack activation approval.

The exact official PP-OCRv6 Small detector and recognizer load and execute with ONNX Runtime 1.30.0, without PaddlePaddle/Paddle, PyTorch or Transformers. Actual downloaded bytes were checked before loading. One unchanged synthetic Chinese/English page produced five detected lines: **4/5 lines matched the ground truth exactly**. The numerical/date line collapsed two double spaces into single spaces. No normalization, threshold changes, alternate models or replacement examples were used to hide that difference. A focused Chinese crop and CTC blank/repeat/space sanity check passed. Ordinary text execution does not establish layout, table or formula fidelity.

## Scope and preserved evidence

This report is the only repository file added by this preflight. No production source, dependency lock, Git index, commit or full project suite was changed/run. The existing progress.md modification belongs to the coordinating task. The Backend Python Expert skill informed isolated runtime use and explicit failure handling; no service/framework was introduced.

Scratch: `D:/agentwork-ocr-preflight-20261004`, created separately from the repository and the prior E5 scratch. CPython 3.12.10 x64 venv has `include-system-site-packages = false`. Six common wheels were read from `D:/agentwork-e5-preflight-20261004/wheels` only after actual size/SHA-256 verification against official PyPI metadata, then copied into the new scratch. The E5 venv, models and evidence were not modified or executed.

Evidence files:

- `acquire.py`, `acquisition.json`, `metadata/`: bounded acquisition, exact source URLs/revisions, sizes, SHA-256, official metadata, resolved dependency declarations, cumulative byte counter and preserved first-attempt metadata. GitHub commit API returned HTTP 403; official `git ls-remote https://github.com/PaddlePaddle/PaddleOCR.git refs/heads/main` resolved the source revision instead. All model downloads succeeded without retries.
- `model/det/` and `model/rec/`: pinned ONNX, inference.yml and model cards. `upstream/`: pinned source snippets and Apache-2.0 LICENSE.
- `requirements.lock`, `wheels/`, `venv/`: complete ten-package CPU/image/geometry experiment closure, installed offline with `--no-index --require-hashes --only-binary=:all:`. `pip check` returned “No broken requirements found.” This lock is external scratch only.
- `preflight.py`, `runtime-results.json`: executable probe, complete graph introspection, exact ground truth and font identity, every box, detector score, recognizer confidence, argmax IDs/confidences, input/output shape, probability range and timings.
- `synthetic-page.png`, `det-map.npy`, `line-0.png` through `line-4.png`, corresponding `line-*-raw.npz`, `focused-crop.png`, `focused-crop-raw.npz`: original page, full detector probability map, rectified crops and full recognizer probability tensors. All text is synthetic.
- `dictionary.txt`: exact ordered YAML dictionary projected to UTF-8 lines, 74,947 bytes, SHA-256 `b5f2bfe2bdd9448429e3e82b51c789775d9b42f2403d082b00662eb77e401c5d`. This is derived evidence, not a separately downloaded dictionary.

Reproduction command (preserve the existing result files before rerunning because the probe overwrites its named outputs):

```powershell
& 'D:/agentwork-ocr-preflight-20261004/venv/Scripts/python.exe' -X utf8 'D:/agentwork-ocr-preflight-20261004/preflight.py'
```

The command rehashes all model/config/source artifacts before loading and uses local files only. Exit 0 is not an exact-text verdict; inspect `status=runtime_passed_with_concerns` and the unchanged comparison. Only one inference run was made. Source AST parsing emitted three invalid-escape SyntaxWarnings in unrelated upstream functions; those functions were not extracted or run.

Evidence SHA-256: `acquire.py` = `5014d42bedf5baec85c795c722a68ed3dad58ce12846640e78b460f1ae6b3b18`; `preflight.py` = `0effd74f1ad6dbe29d8cb1b043a8d8e3fb03e85523a65db39edb9dbcabf86459`; `acquisition.json` = `200a29d707b9f513f5c98f8e28babe0be8117914c523fc9010ae1b24412680de`; `runtime-results.json` = `dab1a256d3d51da674beef6c6ec45562e2169a97695f64da1139920e8f0dde17`; `synthetic-page.png` = `eb69978004c292ae872c1121e3f1829837f40308b513803f00d9a626ac16f8b2`.

## Actual artifact identities and budget

Detector: official `PaddlePaddle/PP-OCRv6_small_det_onnx`, revision `28fe5895c24fd108c19eb3e8479f4ab385fbfc62`. Recognizer: official `PaddlePaddle/PP-OCRv6_small_rec_onnx`, revision `b8f84f0b80c529de40b4fbb3544b84fa7233a513`. Both pinned model cards declare Apache-2.0. [Detector revision](https://huggingface.co/PaddlePaddle/PP-OCRv6_small_det_onnx/tree/28fe5895c24fd108c19eb3e8479f4ab385fbfc62), [recognizer revision](https://huggingface.co/PaddlePaddle/PP-OCRv6_small_rec_onnx/tree/b8f84f0b80c529de40b4fbb3544b84fa7233a513).

Every download used `https://huggingface.co/<repo>/resolve/<full-revision>/<file>`. API revisions matched; weights matched the API LFS SHA-256 and previously reviewed hashes. Small files additionally matched Git blob SHA-1 from the pinned API, and their actual SHA-256 was recorded independently:

| Artifact | Actual bytes | Actual SHA-256 |
| --- | ---: | --- |
| det/inference.onnx | 9,880,512 | `d73e0058b7a8086bbd57f3d10b8bcd4ff95363f67e06e2762b5e814fe9c9410e` |
| det/inference.yml | 885 | `193f435274bf9f0b5f71a929bbfbcf148282df7e633b34e7c373e8f44741b516` |
| det/README.md | 16,076 | `e9403667f0f92055d8ce9e73a2beb1002fc489d9342697db74039eb7178bb5fb` |
| rec/inference.onnx | 21,159,378 | `5435fd747c9e0efe15a96d0b378d5bd157e9492ed8fd80edf08f30d02fa24634` |
| rec/inference.yml | 150,579 | `ab078671bb49f06228eadccd34f1bb501e157f7a047095ffb943ba81512c77d1` |
| rec/README.md | 16,585 | `7d46dbd183ac09bd5a2e6b4577dcb29240751b1c00eb6813eca308d529f86806` |

Weights total **31,039,890 bytes**; weights plus both configs **31,191,354 bytes**; all six model artifacts including cards **31,224,015 bytes**. These are not installed application or installer sizes.

Disk was checked before acquisition (about 58.5 GiB free, above a 3,000,000,000-byte scratch reserve). The persisted acquisition attempt recorded 62,669,049,856 free bytes. Cumulative new response bodies counted by the script total **90,411,698 bytes**, below the **400,000,000-byte cap**. New wheels account for 52,880,579 bytes; all ten verified wheels total 80,527,011 bytes including reused copies. The counter includes successful metadata/source/model/wheel reads and excludes HTTP/TLS overhead, the unread HTTP-403 error body, browser source viewing and the small Git ref advertisement. It is not network wire accounting. No other model, orientation/unwarping model or provider was downloaded/called.

## Runtime closure and license evidence

Each exact filename, immutable files.pythonhosted.org URL, byte size, actual SHA-256 and metadata dependency list is in `acquisition.json`; all hashes are also in `requirements.lock`. Metadata sources follow `https://pypi.org/pypi/<package>/<version>/json`, e.g. [ORT 1.30.0](https://pypi.org/pypi/onnxruntime/1.30.0/json), [OpenCV headless 5.0.0.93](https://pypi.org/pypi/opencv-python-headless/5.0.0.93/json), [Shapely 2.1.2](https://pypi.org/pypi/shapely/2.1.2/json).

| Package | Pin | Wheel bytes | Declared project license |
| --- | --- | ---: | --- |
| onnxruntime | 1.30.0 | 14,311,470 | MIT |
| numpy | 2.5.3 | 12,567,828 | BSD-3-Clause / bundled notices |
| flatbuffers | 25.12.19 | 26,661 | Apache-2.0 |
| packaging | 26.3 | 129,956 | Apache-2.0 OR BSD-2-Clause |
| protobuf | 7.36.2 | 456,514 | BSD-3-Clause |
| pyyaml | 6.0.3 | 154,003 | MIT |
| opencv-python-headless | 5.0.0.93 | 43,825,962 | Apache-2.0 / bundled notices |
| pillow | 12.3.0 | 7,227,137 | MIT-CMU / bundled notices |
| pyclipper | 1.4.0 | 104,619 | MIT |
| shapely | 2.1.2 | 1,722,861 | BSD-3-Clause / bundled GEOS |

Concrete packaging concern: the installed Shapely wheel's `shapely-2.1.2.dist-info/licenses/LICENSE_GEOS` declares bundled GEOS **LGPLv2.1**. OpenCV's `cv2/LICENSE-3RD-PARTY.txt` states FFmpeg is redistributed in all opencv-python packages and contains LGPL notices. The wheels retain those notices, plus ORT third-party and NumPy/Pillow notices. Project-level permissive labels alone are insufficient for a redistributable application license inventory. This preflight does not decide redistribution obligations or approve that production closure. Pillow was used for synthetic rendering; not every probe dependency must ship in the application.

## Verified graph contract

Concrete protobuf inspection and actual ORT introspection found no external tensor references, non-default operator domains, nested attribute graphs or sparse initializers. This was a focused parser for these exact graphs, not a general ONNX checker. Actual successful ORT loading provides separate runtime evidence. Detector has IR 10, default opset 14, 242 nodes and 169 initializers; recognizer has IR 6, default opset 11, 481 nodes and 241 initializers. Both producer strings are empty.

| Graph | Direction / actual name | Type | Actual declared shape |
| --- | --- | --- | --- |
| Detector | input `x` | tensor(float) | `[DynamicDimension.0, 3, DynamicDimension.1, DynamicDimension.2]` |
| Detector | output `fetch_name_0` | tensor(float) | `[ConvTranspose_459_o0__d0, 1, ConvTranspose_459_o0__d2, ConvTranspose_459_o0__d3]` |
| Recognizer | input `x` | tensor(float) | `[DynamicDimension.0, 3, 48, DynamicDimension.1]` |
| Recognizer | output `fetch_name_0` | tensor(float) | `[DynamicDimension.0, Reshape_471_o0__d2, 18710]` |

Names were read from the graphs, not assumed. Detector actual input `[1,3,800,1216]` produced `[1,1,800,1216]`, finite range 0–0.99979603. Recognizer line widths were 656, 694, 933, 818 and 778; outputs were respectively `[1,82,18710]`, `[1,87,18710]`, `[1,117,18710]`, `[1,102,18710]`, `[1,97,18710]`. These are already probabilities; the adapter must not blindly add another softmax. Full probability ranges and sum errors are in the raw results.

The YAML contains **18,708 unique ordered dictionary entries**, with no space entry. The published PaddleOCR CTC constructor appends a space when `use_space_char=True`, then prepends blank: blank index **0**, embedded entries **1–18708**, space index **18709**, total **18,710**. This exactly matches the real graph output. Using a different generation's dictionary or forgetting the appended space would violate this contract. The derived dictionary preserves UTF-8 order and includes multilingual/emoji entries; this probe tests only Chinese/English/numbers.

## Published preprocessing, postprocessing and parity limits

Source revision: `PaddlePaddle/PaddleOCR` **`dab3fe35379033fdcb2d0e9572fac0b36c9a9ebf`**, acquired as immutable raw GitHub files. `acquisition.json` contains exact source URLs, bytes and digests. [Pinned DB postprocess](https://github.com/PaddlePaddle/PaddleOCR/blob/dab3fe35379033fdcb2d0e9572fac0b36c9a9ebf/ppocr/postprocess/db_postprocess.py), [CTC decoder](https://github.com/PaddlePaddle/PaddleOCR/blob/dab3fe35379033fdcb2d0e9572fac0b36c9a9ebf/ppocr/postprocess/rec_postprocess.py), [resize operators](https://github.com/PaddlePaddle/PaddleOCR/blob/dab3fe35379033fdcb2d0e9572fac0b36c9a9ebf/ppocr/data/imaug/operators.py), [recognition resize](https://github.com/PaddlePaddle/PaddleOCR/blob/dab3fe35379033fdcb2d0e9572fac0b36c9a9ebf/ppocr/data/imaug/rec_img_aug.py), [crop geometry](https://github.com/PaddlePaddle/PaddleOCR/blob/dab3fe35379033fdcb2d0e9572fac0b36c9a9ebf/tools/infer/utility.py), [dictionary loading/default space behavior](https://github.com/PaddlePaddle/PaddleOCR/blob/dab3fe35379033fdcb2d0e9572fac0b36c9a9ebf/tools/infer/predict_rec.py).

The probe AST-extracts only named classes/functions from these preserved, hash-verified sources, omitting their Paddle tensor-dispatch `__call__` methods and module imports. It directly calls the NumPy/OpenCV methods. No fake Paddle module, Paddle installation or dynamic YAML transform execution is used. This is a transparent experiment mechanism, not a proposed production source loader.

- Detection: RGB synthetic image converted to BGR; default `DetResizeForTest.resize_image_type0` uses minimum side 736, maximum side 4000, rounds dimensions to multiples of 32. Thus the original 1200-pixel width becomes 1216; coordinate ratios are retained. Float32 scale 1/255, mean `[0.485,0.456,0.406]`, standard deviation `[0.229,0.224,0.225]` in existing BGR order, then CHW/batch.
- DBPostProcess: YAML `thresh=0.2`, `box_thresh=0.45`, `unclip_ratio=1.4`, `max_candidates=3000`; upstream defaults `score_mode=fast`, `box_type=quad`, no dilation, minimum side 3. The exact upstream `boxes_from_bitmap` is called, preserving its scores (the normal wrapper discards them). It uses OpenCV contours/min-area rectangles, polygon mask mean, Shapely area/perimeter and Pyclipper round expansion, then source-page coordinate scaling.
- Crop: exact upstream perspective crop, cubic interpolation, replicated border; tall-crop heuristic rotates crops with height/width at least 1.5. It is not a page orientation classifier, and the upright synthetic page does not validate rotated/vertical content.
- Recognition: exact upstream `resize_norm_img_chinese` with `[3,48,320]`, aspect-preserving dynamic width at least 320, BGR CHW, `/255`, subtract 0.5, divide 0.5, zero padding. One crop per call; no batch-padding quality claim.
- CTC: argmax, remove consecutive duplicate IDs before filtering blank, concatenate ordered characters, average selected maximum probabilities. Focused sanity sequence `[blank,A,A,blank,A,space,space,中,blank]` decodes to `AA 中` with expected confidence 0.65.

These directly reused upstream routines plus the real graph/dictionary/output checks remove several guessed contracts. **Full independent pipeline parity is still unproven**: no Paddle/PaddleX reference execution or upstream golden image fixture comparison was performed, and application-owned adapter code does not yet exist. Crop/coordinate behavior beyond this simple page needs focused reference fixtures before production integration.

## Fixed synthetic page and raw outcomes

The page is 1200×800 RGB, white, with five 30-pixel black text lines rendered at `(48, 60 + 125*i)` using existing `C:/Windows/Fonts/msyh.ttc`, face index 0, **Microsoft YaHei Regular**. Font file SHA-256: `d79c55e68b1131eea0cc1c47be4f572d964f28c682e143db2ad09c1e4cb07a3f`. The font was neither downloaded nor redistributed. Visual inspection confirmed legible, unclipped Chinese/English page content. Exact ground truth is also in `preflight.py` and JSON; line 4 below contains two double spaces:

```text
StudyCrew OCR runtime preflight
本地文字识别：北京是中国的首都。
English text: Water freezes at 0 degrees Celsius.
日期 2026-10-04  数量 12345  金额 98.50
中英文 mixed text / CPU only / no cloud
```

| Line | Detector score | Recognition confidence | Exact text result |
| --- | ---: | ---: | --- |
| 1 | 0.8601083846 | 0.9997649789 | Exact |
| 2 | 0.8629276231 | 0.9922142625 | Exact |
| 3 | 0.8195810577 | 0.9987287521 | Exact |
| 4 | 0.8369170614 | 0.9601951838 | `日期 2026-10-04 数量 12345 金额 98.50` — two spaces omitted |
| 5 | 0.8572569791 | 0.9641281962 | Exact |

Full quadrilaterals in source-page pixels, ordered TL/TR/BR/BL, are respectively `[[46,63],[525,65],[525,100],[46,98]]`; `[[46,189],[509,190],[509,222],[46,221]]`; `[[47,315],[728,315],[728,350],[47,350]]`; `[[49,439],[629,439],[629,473],[49,473]]`; `[[46,563],[614,564],[614,599],[46,598]]`. The Chinese detector box does not span the full Pillow nominal text bounding-box width (528); despite correct recognized text, this is a reason not to equate a text match with perfect geometry.

The separate known Chinese line crop, including a fixed six-pixel margin, was `[43,492,3]`; normalized input `[1,3,48,549]`; output `[1,69,18710]`. It matched exactly at confidence 0.9979148507, with maximum probability-row sum error `3.5762786865234375e-07`. This isolates one recognition case from detector/crop expansion; it is not a broad benchmark.

## Resource, offline and remaining release limits

Runtime machine is the same Windows x64 environment described by the separate E5 preflight (Intel Core i5-12450H); this run reports Windows 10.0.26300 and Python 3.12.10. ORT explicitly selected and returned **CPUExecutionProvider only**, disabled fallback/telemetry, used two intra-op threads, one inter-op thread, sequential execution, no profiling, no optimized-model output and no custom-op/provider plugin registration. OpenCV threads were capped at one. Probe limits are 1.5 million page/crop pixels, at most 30 detected crops, recognizer width at most 2048; actual page was 960,000 pixels and detector tensor 972,800 spatial pixels. These explicit input/work limits do not provide process-level hard RAM or wall-time enforcement. RAM was not measured and no production cancellation guarantee is implied.

Observed session loads: detector 0.111 s, recognizer 0.090 s. Detector inference 0.209 s; five recognition calls 0.027–0.053 s each; focused crop 0.023 s. These are single-run observations including normal machine variability, not latency promises or benchmark statistics.

Before runtime imports, the probe set offline/telemetry environment flags, called ORT's telemetry-disable API and installed a Python audit hook rejecting socket connect/DNS/sendto; zero attempts were recorded. No Paddle/Torch/Transformers modules were imported. A Python hook cannot intercept every native socket; native/OS denied-network verification remains open. No provider, user source, account or cloud OCR participated.

Remaining gates: independently verify adapter/preprocess/DB/crop/CTC parity on reference fixtures; evaluate ordinary real-world OCR and spacing/geometry failures without claiming table/formula understanding; review complete native/bundled license notices; measure frozen sidecar execution and footprint on clean supported Windows CPUs; test missing DLLs, corrupt files, denied network, memory limits and cancellation. The exact model contract is now usable for implementation preparation. Automatic activation, frozen distribution readiness and formal quality acceptance remain unapproved by this experiment.
