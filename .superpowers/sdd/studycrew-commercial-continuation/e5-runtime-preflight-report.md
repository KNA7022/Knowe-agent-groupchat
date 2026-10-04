# E5 real-runtime preflight — passed with concerns

Date: 2026-10-04. This report records an isolated Windows CPU experiment, not production implementation or model activation approval.

The exact official E5 INT8 candidate loads and executes on this machine with CPU ONNX Runtime 1.30.0 and Tokenizers 0.23.2. Actual artifact bytes and SHA-256 were verified before session creation. The graph produces token embeddings and requires external attention-mask mean pooling followed by L2 normalization. Finite 384-dimensional vectors, tokenizer normalization, right padding, empty-prefix input and 512-token execution were verified.

**The synthetic ranking smoke check did not fully pass:** the original batched run ranked the intended passage first for 7 of 8 queries. Re-encoding the identical texts individually ranked 6 of 8 first and changed one cross-language result. No examples or thresholds were changed to improve the result. These eight examples do not estimate product Recall, formal RAG golden-set performance or baseline-fleet compatibility. Retain a quality gate before default activation.

## Scope and preserved evidence

Only this report was added to the repository by this preflight. No production source, tests, dependency locks, Git index or commits were modified. No full project tests, provider calls, user-source processing, BGE/Paddle/PyTorch/Transformers downloads, alternative model downloads or global installs occurred. The Backend Python Expert skill informed the isolated Python runtime and explicit failure handling; this experiment did not introduce a framework or production service.

Preserved scratch directory: `D:/agentwork-e5-preflight-20261004` (outside the repository). It contains:

- `acquire.py`, `acquisition-results.json`, `metadata/`: acquisition logic, actual byte counters, successful request hashes, nested failed-attempt evidence and official metadata.
- `wheels/`, `requirements.lock`, `install_verified.py`, `install.log`: exact locally verified wheel closure and offline installation evidence.
- `model/`: only the pinned E5 candidate artifacts listed below.
- `venv/`: isolated Python 3.12 environment; no system-site-packages.
- `preflight.py`, `runtime-results-first.json`, `runtime-results.json`: executable probe, preserved original batched result and follow-up containing the same cases plus singleton diagnostics. All synthetic candidate texts, query texts, complete score matrices, rankings, token examples, runtime versions and timings are recorded.
- `cpu-platform.json`: processor, CPUID register values and pip consistency check.

Reproduction uses `D:/agentwork-e5-preflight-20261004/venv/Scripts/python.exe -X utf8 D:/agentwork-e5-preflight-20261004/preflight.py`. This rehashes all model artifacts and uses local files only. It writes the structured status to `runtime-results.json`; process exit 0 alone is **not** the ranking verdict (`status` is `retrieval_failed`). Preserve current evidence before rerunning. No cleanup was performed.

## Tested hardware and runtime

Windows build `10.0.26300.0`; Intel Core i5-12450H, 8 physical cores / 12 logical processors; CPython `3.12.10`, x64, MSC v.1943. Initial free space on D was approximately 63.14 GB, above a 3 GB scratch reserve. ORT build: `git-branch=HEAD, git-commit-id=f2c39fe, fp8-kv-cache=1, build type=Release`.

Direct `.NET X86Base.CpuId` readings:

| CPUID | EAX | EBX | ECX | EDX |
| --- | ---: | ---: | ---: | ---: |
| leaf 7, subleaf 0 | 2 | 597436329 | 406849444 | -1139227632 |
| leaf 7, subleaf 1 | 4196368 | 0 | 0 | 0 |

AVX2 and AVX-VNNI are present. AVX512F and AVX512-VNNI are absent. NumPy's runtime feature detection independently reports AVX2 present and AVX-512 variants absent. This proves that the filename `avx512_vnni` is not a hard AVX-512 requirement for this ORT session on this CPU; **it does not establish compatibility or accuracy on CPUs without VNNI**. The exact native kernel selected was not profiled. This machine is not the supported baseline fleet or a clean frozen-package machine.

Session options used two intra-op threads, one inter-op thread and sequential execution. Only `CPUExecutionProvider` was explicitly selected and returned; provider fallback was disabled. ORT telemetry was disabled before session creation; profiling was false and optimized-model output path was empty. No custom-op library or provider plugin was registered. Session construction took about 0.30 seconds in the first probe; first-run synthetic inference took about 14 ms for 6 passages, 21 ms for 8 queries and 125 ms for one 512-token input. These are single-run observations, not benchmarks or latency commitments. RAM, frozen size and installer delta were not measured.

## Actual artifact identity and download accounting

Official repository: `intfloat/multilingual-e5-small`; immutable revision `614241f622f53c4eeff9890bdc4f31cfecc418b3`. Each artifact was fetched from `https://huggingface.co/intfloat/multilingual-e5-small/resolve/<revision>/<path>`, with the revision separately checked in the publisher API. The model card declares MIT. A complete redistributable licence/notice package was not prepared.

| Artifact | Actual bytes | Actual SHA-256 |
| --- | ---: | --- |
| `onnx/model_qint8_avx512_vnni.onnx` | 118,346,824 | `dd476dd0c2514e9b9be83aeb3853fac0763e0bdf4a71645407587d77c48a2d88` |
| `tokenizer.json` | 17,082,730 | `0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39` |
| `config.json` | 655 | `69137736cab8b8903a07fe8afaafdda25aac55415a12a55d1bffa9f581abf959` |
| `tokenizer_config.json` | 443 | `a1d6bc8734a6f635dc158508bef000f8e2e5a759c7d92f984b2c86e5ff53425b` |
| `special_tokens_map.json` | 167 | `d05497f1da52c5e09554c0cd874037a083e1dc1b9cfd48034d1c717f1afc07a7` |
| `1_Pooling/config.json` | 200 | `987f7a67a38fa564c849bb5d277c52ab9088a84368fc0be31a354125aebb12a0` |
| `modules.json` | 387 | `c6e29747481e8b5dd2b58401966aeac910de39092f90cda9a704b1545f902b04` |
| `sentence_bert_config.json` | 57 | `948201d8329907aae938fa62f9ceeed53f5694dacc2b87b9f3b78b37ee986529` |
| `README.md` | 497,538 | `0038de97aee16258cecbad7ffda4b4febd6953e747a00e0ddbc8e6ed241e9c1c` |

Total preserved model artifacts: **135,929,001 bytes**, including model-card/config evidence. Weight + tokenizer + the three root configuration JSONs total **135,430,819 bytes**. This is 2 bytes more than the design's 135,430,817 figure: the root `config.json` fetched here is 655 bytes; publisher metadata lists `onnx/config.json` as 653 bytes. Those two paths must not be conflated in a manifest. No SentencePiece fallback was installed.

Actual verified wheel downloads total **36,526,979 bytes**. The acquisition script counted **211,936,407 response-body bytes** across metadata, wheels, model files and successful chunks of failed requests, below the 1,000,000,000-byte cap. This is an application-level byte counter, not network wire traffic; HTTP/TLS overhead and browser documentation fetches are excluded. One wheel TLS connection failed before the successful retry; the first weight request timed out after partial reads, and one bounded weight retry succeeded. A separate HEAD connectivity check returned no response body. All verified artifacts were reused rather than downloaded again.

The 21-package closure was resolved from official PyPI JSON metadata, pinned and installed with `--no-index --require-hashes --only-binary=:all:` from the verified local wheels. `pip check` reported no broken requirements. Root pins were available and matched the design's wheel SHA-256 values; no substitute runtime was used. Tokenizers requires `huggingface-hub<2.0,>=0.16.4`; its compatible published 1.33.0 release was selected, rather than the out-of-range current main version. A scratch resolver initially mishandled a wildcard requirement, was corrected, and was rerun before installation. Exact filenames, URLs, sizes, dependency declarations and all 21 wheel hashes are preserved in `acquisition-results.json` and `requirements.lock`.

| Package | Pin | Wheel bytes |
| --- | --- | ---: |
| onnxruntime | 1.30.0 | 14,311,470 |
| tokenizers | 0.23.2 | 2,863,236 |
| flatbuffers | 25.12.19 | 26,661 |
| numpy | 2.5.3 | 12,567,828 |
| packaging | 26.3 | 129,956 |
| protobuf | 7.36.2 | 456,514 |
| huggingface-hub | 1.33.0 | 846,435 |
| click | 8.5.0 | 125,251 |
| filelock | 4.0.10 | 111,193 |
| fsspec | 2026.9.0 | 221,738 |
| hf-xet | 1.6.0 | 4,033,128 |
| httpx | 0.28.1 | 73,517 |
| pyyaml | 6.0.3 | 154,003 |
| tqdm | 4.70.1 | 80,199 |
| typing-extensions | 4.16.0 | 45,571 |
| anyio | 4.15.1 | 132,079 |
| certifi | 2026.7.22 | 136,983 |
| httpcore | 1.0.9 | 78,784 |
| idna | 3.20 | 69,583 |
| colorama | 0.4.6 | 25,335 |
| h11 | 0.16.0 | 37,515 |

ORT root wheel SHA-256: `f3501472571f1b1eee50e017851e7929f5ea37312d2d8c2494a19e8fc58b4a38`. Tokenizers root wheel SHA-256: `2e96f5699d5249c9c64aa8412e044f727aae3a4098cf830f9901ec1afc361cde`. The Hub/Xet packages are dependency closure only; no Hub download API was called during inference.

## Verified graph and embedding contract

Lightweight inspection of the verified ONNX protobuf plus ORT session introspection found IR version 6, default-domain opset 11, producer `onnx.quantize`, 1,630 nodes and 348 initializers. The graph has 96 `DynamicQuantizeLinear` and 96 `MatMulInteger` nodes and contains signed INT8 initializers. No non-default operator domains, nested attribute graphs, sparse initializers or external tensor references were found by this concrete-graph inspection. This was not a general-purpose ONNX checker; ORT successfully loaded the graph. The export recipe and exact per-layer numerical error were not reconstructed.

| Direction | Name | Type | Shape |
| --- | --- | --- | --- |
| Input | `input_ids` | `tensor(int64)` | `[batch_size, sequence_length]` |
| Input | `attention_mask` | `tensor(int64)` | `[batch_size, sequence_length]` |
| Input | `token_type_ids` | `tensor(int64)` | `[batch_size, sequence_length]` |
| Output | `last_hidden_state` | `tensor(float)` | `[batch_size, sequence_length, 384]` |

The actual query batch output was `[8,15,384]`; pooled output was `[8,384]`. Pooling was applied once, outside the graph:

```text
mask = attention_mask[..., None].astype(float32)
pooled = sum(last_hidden_state * mask, axis=1) / sum(mask, axis=1)
embedding = pooled / L2_norm(pooled, axis=1, keepdims=True)
```

All real tokens, including BOS/EOS, contribute; padding does not. The pinned pooling configuration enables mean pooling only, and the pinned modules list includes Normalize. All output vectors tested were finite. Query norm range was 0.99999988–1.0. Repeating the same padded batch yielded an exact zero maximum absolute difference.

Tokenizer loaded through `Tokenizer.from_file` only. Model type is Unigram, with the published Sequence normalizer, Metaspace pre-tokenizer and TemplateProcessing special-token insertion. Prefixes were `query: ` and `passage: ` for both Chinese and English. The saved tokenizer has no enabled padding/truncation initially; the probe explicitly enabled right padding to the longest batch member and right truncation at 512 total tokens including special tokens. Single-sequence type IDs are 0. Actual special IDs: BOS 0, PAD 1, EOS 2, UNK 3, MASK 250001. **Use tokenizer PAD 1, not `config.json`'s model-level `pad_token_id=0`.** Padding positions were checked to contain ID 1 with mask 0.

Composed/decomposed “café”, full-width/ASCII “ＡＢＣ”/“ABC”, and repeated/single spaces produced equal respective token ID sequences. Raw empty text tokenizes to special tokens; prefix-only query/passage inputs produced finite `[2,384]` output. A synthetic 4,205-token passage was truncated to 512 ending with EOS 2, with overflow reported, and executed successfully. This only validates bounded truncation behavior; product chunking must preserve the discarded text and citations rather than silently truncating sources. Transformers/SentencePiece reference parity was not independently tested because those downloads were outside this preflight.

## Ranking evidence and remaining concerns

Candidate passage IDs, unchanged across both probes:

| ID | Synthetic passage |
| --- | --- |
| P0 | 北京是中国的首都。 |
| P1 | Paris is the capital of France. |
| P2 | 植物通过光合作用利用阳光制造养分。 |
| P3 | Water freezes at zero degrees Celsius. |
| P4 | Cats are small domesticated animals. |
| P5 | 数据库索引可以加快数据查询速度。 |

| Synthetic query | Expected | Batched first | Singleton first |
| --- | --- | --- | --- |
| 中国的首都是哪里？ | P0 | P0 | P0 |
| What is the capital of France? | P1 | P1 | P1 |
| 植物如何利用阳光制造养分？ | P2 | P2 | P2 |
| At what temperature does water freeze? | P3 | P3 | P3 |
| Which animals are kept as small domestic cats? | P4 | P4 | P4 |
| 怎样加快数据库查询速度？ | P5 | P5 | P5 |
| What is the capital of China? | P0 | P0 | P1 |
| 法国的首都是哪里？ | P1 | P0 | P0 |

For the last query, batched scores were P0 0.793109, P5 0.777104, P1 0.775386, P2 0.775266, P3 0.673763, P4 0.662700. Its correct Paris passage ranked third. Singleton scores for that query were P0 0.807928, P2 0.788583, P5 0.787218, P1 0.777097, P3 0.677482, P4 0.672475; Paris ranked fourth. For the English China query, the original batch favored Beijing 0.784217 over Paris 0.779690, whereas singleton encoding favored Paris 0.786529 over Beijing 0.784269. Full precision scores for all candidate/query pairs remain in both raw result files.

Query singleton-versus-batched embedding cosine ranged from 0.993291 to 0.997541; passage cosine ranged from 0.996723 to 0.998007. The observed batch dependence is a concrete concern. Dynamic quantization recomputes activation parameters and is a possible contributor, but this preflight does not establish the cause or separate quantization error from model limitations. No FP32 comparator, different model or wider benchmark was downloaded. ORT's [quantization documentation](https://onnxruntime.ai/docs/performance/model-optimizations/quantization.html) explains activation-dependent dynamic quantization and possible accuracy loss; it does not validate these particular rankings.

Before runtime imports, the probe set `HF_HUB_OFFLINE=1`, `HF_HUB_DISABLE_TELEMETRY=1`, `TRANSFORMERS_OFFLINE=1`, `DO_NOT_TRACK=1`, `TOKENIZERS_PARALLELISM=false`, and `ORT_DISABLE_TELEMETRY=1`; it also called ORT's telemetry-disable API. A Python audit hook rejected socket connect/DNS/sendto operations and recorded zero attempts. These checks do not intercept every native socket or prove OS-level network isolation. No user data was used; only explicit synthetic strings entered inference. A denied-network clean-machine frozen sidecar test, native telemetry/socket inspection, dependency/licence review and supported-CPU quality validation remain required.

This result supports implementing the verified graph/tokenizer/pooling contract and retaining an unavailable/concerns state. It does not authorize automatic pack activation, product packaging, a model substitution, cloud fallback or a passing formal RAG claim.

Primary upstream evidence: [pinned model repository](https://huggingface.co/intfloat/multilingual-e5-small/tree/614241f622f53c4eeff9890bdc4f31cfecc418b3), [pinned model card](https://huggingface.co/intfloat/multilingual-e5-small/raw/614241f622f53c4eeff9890bdc4f31cfecc418b3/README.md), [ORT 1.30.0 metadata](https://pypi.org/pypi/onnxruntime/1.30.0/json), [Tokenizers 0.23.2 metadata](https://pypi.org/pypi/tokenizers/0.23.2/json). Actual local measurements and hashes above take precedence over metadata-only availability claims.
