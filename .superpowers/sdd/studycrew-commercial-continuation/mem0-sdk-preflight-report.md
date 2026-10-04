# Mem0 OSS SDK bounded preflight — actual execution

Date: 2026-10-04. Status: **constructor/extension feasibility established in scratch; production integration and security acceptance remain unimplemented**.

This report is the only project-file output. No application source/tests/requirements/lock/index, global Python installation, Git commit, full-project test, cloud account, Docker service, model weights, or production data were changed/used. Backend Python Expert guidance informed normal Pydantic boundary validation and resource ownership. The SDK implementation itself was neither copied nor patched.

## Scope and environment

- External scratch: `D:/mem0-sdk-preflight-20261004-a1`; interpreter: `venv/Scripts/python.exe`, CPython **3.12.10**. Fresh standard venv from `E:/python/python.exe`, without system site packages.
- Checked free space before setup: D: **62,377,803,776 bytes**; C: **24,394,100,736 bytes**; E: **264,702,640,128 bytes**.
- Source wheel: `D:/agentwork/wheelprobe-mem0/mem0ai-2.0.20-py3-none-any.whl`; SHA-256 **655103616fe70b67e0434f6725aad35269a071adcd1f61426553b10e0b1b076c**.
- Reused 36 cached wheels, **35,893,272 bytes**, from `D:/agentwork/wheelprobe-mem0-full`. Each filename/hash was independently compared to its official `https://pypi.org/pypi/{name}/{version}/json` release record, checking the artifact URL uses `files.pythonhosted.org`. Only official package metadata was fetched; **zero new wheel or model downloads**. Total stayed below the authorized 500 MB bound.
- Installed only the base dependency closure into this venv with `--isolated --no-index --no-cache-dir`. `pip check` returned **No broken requirements found.**
- Actual SDK: `mem0ai==2.0.20`, `Memory.__module__ == "mem0.memory.main"`; Pydantic **2.13.5** / core **2.46.5**.
- spaCy, torch, transformers and paddle were verified absent before import. No NLP extras or models were installed. The wheel's `spacy_models.py` has automatic download behavior when spaCy exists without its model; no additional official disable flag was found in that inspected path. This probe exercised the actual SDK's absent-spaCy fallback.

## Reproduction commands and retained evidence

Executed from PowerShell 7 with UTF-8 reads and logs:

```powershell
Get-PSDrive -PSProvider FileSystem
Get-FileHash -Algorithm SHA256 'D:/agentwork/wheelprobe-mem0/mem0ai-2.0.20-py3-none-any.whl'
New-Item -ItemType Directory -Path 'D:/mem0-sdk-preflight-20261004-a1'
& 'E:/python/python.exe' -m venv 'D:/mem0-sdk-preflight-20261004-a1/venv'
& 'D:/mem0-sdk-preflight-20261004-a1/venv/Scripts/python.exe' 'D:/mem0-sdk-preflight-20261004-a1/verify_wheels.py' | Tee-Object -FilePath 'D:/mem0-sdk-preflight-20261004-a1/wheel-verification.json' -Encoding UTF8
& 'D:/mem0-sdk-preflight-20261004-a1/venv/Scripts/python.exe' -m pip install --isolated --no-index --no-cache-dir --find-links 'D:/agentwork/wheelprobe-mem0-full' 'D:/agentwork/wheelprobe-mem0/mem0ai-2.0.20-py3-none-any.whl' --report 'D:/mem0-sdk-preflight-20261004-a1/install-report.json'
& 'D:/mem0-sdk-preflight-20261004-a1/venv/Scripts/python.exe' -m pip check
& 'D:/mem0-sdk-preflight-20261004-a1/venv/Scripts/python.exe' -B 'D:/mem0-sdk-preflight-20261004-a1/probe.py' 2>&1 | Tee-Object -FilePath 'D:/mem0-sdk-preflight-20261004-a1/probe-run-4.log' -Encoding UTF8
```

Scratch scripts were authored using `apply_patch`; no installed dependency files were modified. Evidence hashes:

| Scratch file | SHA-256 |
| --- | --- |
| probe.py | dfca1c79b54b76ba4e8614092015d6a33f4f916d0ee77df43596e31f52f604c7 |
| verify_wheels.py | dfc54481ca66f0e33fc44706907058366d4e48334773c1943e069a6d757ca331 |
| probe-run-4.log | 37eb56f616a1e87997e424a491de421543a0f5929f1de6910a4ad10d2b548179 |
| wheel-verification.json | 8f1d6a90cc58819635103c3a3475e1b7216e4fcf91593db34ac14f4fd39985ed |
| install-report.json | 90830f2e7e50027e57f240d62937c0c8ff3216eed56227b3bb3d385071000d74 |

The scratch environment and synthetic logs remain available for review. Earlier `probe-run-1.log`, `probe-run-2.log`, `probe-run-3.log` preserve harness corrections: (1) replacing the Popen class with a function broke asyncio's Windows subclass definition; retained the real class and used its preinstalled audit denial instead; (2) cleanup assumed a non-null base HTTP client; corrected after inspecting `build_http_client`; (3) an assertion incorrectly equated Qdrant module import with backend construction; replaced by a pre-import constructor tripwire. An earlier wheel-inspection one-liner had an unmatched parenthesis and performed no import. The final run exits **0**.

## Actual SDK construction and calls

The scratch `MC/VC/LC/EC` classes subclass the installed `MemoryConfig/VectorStoreConfig/LlmConfig/EmbedderConfig`. They use ordinary Pydantic validation, narrowed literal provider fields, required configs, `extra='forbid'`, and same-name inherited validator overrides. Vector nested config remains a real typed model through typed construction, `model_dump()`, dict reconstruction and repeated validation. No `model_construct`, default-config mutation, built-in provider overwrite or fake `mem0.*` module is used.

The unique LLM provider is registered through `LlmFactory.register_provider(..., Options)`; embedder/vector class paths use the pinned factory maps. The LLM and embedder implement installed base interfaces with deterministic synthetic fixtures; the vector class implements installed `VectorStoreBase` using locked Python dictionaries and finite four-dimensional cosine scores. It is a scratch exact-scan fixture, **not USearch and not a production adapter**. The installed SDK's actual `SQLiteManager` is retained with `history_db_path=':memory:'`; there is no supported separate history-provider registration exercised here. Thus history is the real SDK in-memory implementation, not a replaced class.

Actual calls in the successful run:

- `Memory(config=validated_config)` creates distinct approved, candidate, failure-test and rebuilt instances. `PRAGMA database_list` is `[(0, 'main', '')]`; `PRAGMA temp_store=MEMORY` is set before content operations.
- `approved.add("Synthetic orb is blue.", user_id="fixture_subject", infer=False, metadata=...)` twice: two records, zero LLM calls. **Direct mode does not exact-deduplicate.**
- `candidate.add(..., infer=True)` twice, deterministic LLM JSON yielding three facts including one exact within-batch duplicate: first call two ADDs; second call zero; two actual LLM adapter calls. This confirms the real SDK's exact-hash dedup against retrieved candidates and within batch, not semantic truth or unrestricted dedup.
- `approved.search(..., filters={"user_id":"fixture_subject"}, top_k=10, threshold=0, rerank=False)`, additional wrong-user and wrong-group searches, `approved.get(candidate_id)`, adapter unfiltered list: candidate red-orb data never appears in approved instance; approved and candidate have physically distinct dictionary arenas. Same subject identifier was intentionally used in both, so separation does not depend on status/user metadata filtering.
- Actual `candidate._upsert_entity(...)` and lazy `entity_store` trigger SDK deep-copy/reconstruction and collection-name mutation into `c_001_entities`; approved entity collection `a_001_entities` stays empty. Directly invoking this SDK hook avoids pretending absent NLP emitted entities. Natural entity extraction and entity-enhanced relevance remain untested.
- `candidate.delete(id)`, `candidate.get(id)`, `candidate.history(id)`: vector and linked synthetic entity are removed; history retains ADD/DELETE and deleted old content **in RAM**.
- `Memory.close()`, explicit vector/entity disposal and conditional client close release scratch arenas. New `Memory` starts empty; approved-only synthetic text is reprojected with `infer=False`. This is in-process close/reopen/rebuild from a fixture list, **not a process-crash test or real encrypted-repository integration**.

Provider, arbitrary path, LLM key, embedder URL, disk history path, migration collection name and omitted embedder all failed ordinary validation. Config clone support is verified for these test classes at this exact dependency set; no production collision/idempotence/version-upgrade guarantee follows.

## Preserved failure behavior and integration consequences

| Injected/observed case | Actual SDK result | Projection/history state | Consequence for later integration |
| --- | --- | --- | --- |
| LLM returns `not-json` | `{"results":[]}` | No vector fact inserted; SDK empty-extraction path may retain messages in its in-memory history | Strictly validate model output before returning it to Mem0; empty result alone does not establish valid extraction |
| LLM adapter raises fixture error | `LLMError` propagates | No new vector fact | Keep sanitized sticky operation failure and do not log raw SDK errors |
| Every vector insert raises, including SDK individual fallback | **One ADD result is returned** | **Zero vector rows, one ADD history row** | Do not treat SDK ADD result as successful projection/commit |
| Same direct text added twice with `infer=False` | Two ADDs | Two vector records | Durable repository exact dedup and rebuild mapping remain necessary |
| Delete a candidate with linked entity | get returns None, entity removed | Old content survives in in-memory history | Dispose the full arena/history for application deletion policy; SDK delete alone is insufficient |

The failing vector fixture uses a persistent `insert_fail` switch and raises from every insert entry point. A production adapter can set a sticky failed flag at the caught boundary and expose it to the service; the service must reject/discard the whole batch after the SDK returns whenever that flag is set, and verify expected projection IDs before committing authority. **A production sticky-failure service and repository transaction were not implemented or tested here.** No SDK fallback was repaired or bypassed.

The additive pipeline leaves approved blue-orb memory untouched while adding candidate red-orb memory in a different arena. This does not demonstrate same-arena contradiction reconciliation; source inspection and this additive result do not justify claiming autonomous correction or semantic dedup.

Two design corrections established by execution/source follow-up:

1. `BaseEmbedderConfig` calls `build_http_client(None)`, which returns **None** when proxy configuration is absent. The design's unconditional unused-client assumption is inaccurate for this pinned wheel/config; cleanup must be conditional. No client was sent through in this probe.
2. SDK import constructs default configuration models and imports **qdrant_client types/modules**, even when explicit custom instances are later used. The meaningful no-backend assertion is **zero Qdrant constructor calls**, not zero Qdrant imports. A sys/thread profile tripwire was active before SDK import for the sync/async Qdrant client, local Qdrant store and Mem0 Qdrant wrapper constructors; none ran.

## Network and filesystem observations

Before the first transitive Mem0 import, the script sets `MEM0_TELEMETRY=False`, sample rate 0 and a private `MEM0_DIR`. Python audit denial and socket/DNS/send, urllib, requests, httpx/httpx2 send and subprocess entry denial are installed before SDK import. Popen uses the Python audit hook because replacing its class breaks asyncio. These guards remain active during all calls and shutdown. Only fixture strings and vectors are supplied; there are no provider keys or real personal data.

Successful final run: **zero observed denied network/process attempts**, no PostHog client or OSS telemetry singleton, zero Qdrant constructors, only MainThread at completion, zero remaining vector arenas. All four SDK objects used in-memory SQLite. Runtime write audit permits only the dedicated runtime path and denies file-backed SQLite. Final runtime directory `D:/mem0-sdk-preflight-20261004-a1/runtime-baa094b9` contains only `config.json` with a random `user_id`; a temporary config file was atomically replaced. No synthetic content was present in that runtime file. No home `.mem0` path was selected.

This is **in-process network denial/instrumentation, not a Windows firewall, packet capture or packaged-runtime sandbox proof**. Native extensions that bypass Python networking were not adversarially tested; Qdrant/native model clients were not constructed. Denial guards were installed but not independently exercised with a guard self-test. The artifact scan is bounded to the owned runtime directory plus audited Python writes, not a forensic disk scan. Scratch source/logs intentionally contain synthetic fixture strings. No application PII/security acceptance or plaintext-erasure claim follows.

## Raw successful run

```jsonl
{"check": "runtime", "result": {"python": "3.12.10", "mem0ai": "2.0.20", "pydantic": "2.13.5", "Memory_module": "mem0.memory.main"}}
{"check": "constructor", "result": {"typed_dict_revalidate": true, "sqlite_database_list": [[0, "main", ""]], "history_class": "SQLiteManager", "embed_http_client": "None"}}
{"check": "closed_configs", "result": ["provider", "path", "llm_key", "embed_url", "history", "migration_collection", "missing_embedder"]}
{"check": "direct_add_and_duplicate", "result": {"adds": 2, "llm_calls": 0}}
{"check": "extraction_dedup", "result": {"first_count": 2, "repeat_count": 0, "llm_calls": 2, "events": ["ADD", "ADD"]}}
{"check": "separate_instances_namespace_filter", "result": {"approved_count": 2, "candidate_count": 2, "foreign_get": null, "no_filter_adapter_counts": [2, 2]}}
{"check": "lazy_entity_clone", "result": {"candidate_entities": 1, "approved_entities": 0, "config_type": "VectorOptions"}}
{"check": "delete", "result": {"get": null, "entity_rows": 0, "history_events": ["ADD", "DELETE"], "history_retains_old_synthetic_content": true}}
{"check": "malformed_llm", "result": {"results": []}}
{"check": "llm_failure", "result": "LLMError"}
{"check": "insert_failure", "result": {"sdk_result_count": 1, "stored_count": 0, "history_count": 1}}
{"check": "reopen_rebuild", "result": {"initial_rows": 0, "rebuilt_approved": 1, "new_sqlite_history_rows": 1}}
{"check": "sideeffects", "result": {"network_or_process_attempts": [], "runtime_path": "D:\\mem0-sdk-preflight-20261004-a1\\runtime-baa094b9", "runtime_files": ["config.json"], "config_keys": ["user_id"], "qdrant_imported": true, "qdrant_constructor_calls": [], "posthog_client": null, "remaining_arenas": 0, "writes": ["D:\\mem0-sdk-preflight-20261004-a1\\runtime-baa094b9", "D:\\mem0-sdk-preflight-20261004-a1\\runtime-baa094b9\\tmp1nsbq__x"], "threads": ["MainThread"]}}
{"check": "calls", "result": {"llm": 5, "embed": 19, "batch": 3, "vector": 6}}
```

## Exact verified wheel closure

These are the cached, installed versions and SHA-256 values verified against official release metadata. Installation tooling bundled by venv (pip) is not part of this wheel closure. Full filenames and byte counts are retained in `wheel-verification.json`; all wheel paths remain outside the project.

| Wheel filename | SHA-256 |
| --- | --- |
| annotated_types-0.8.0-py3-none-any.whl | f072f4d804ea359e4eaf198b1af7a8b0943881a87f31bb764f8bf219bb9419e0 |
| anyio-4.15.1-py3-none-any.whl | 6152fdbbf9a77fdec97731721bebf7c4c44f7c29b424b0065826173efc7ed101 |
| backoff-2.2.1-py3-none-any.whl | 63579f9a0628e06278f7e47b7d7d5b6ce20dc65c5e96a6f3ca99a6adca0396e8 |
| certifi-2026.7.22-py3-none-any.whl | 62f22742b58a1a33014a2b6b706588a8d7e2a88ae7bd1a6ebe8c992928483775 |
| charset_normalizer-3.5.1-cp312-cp312-win_amd64.whl | 3617ac3cfd8b9888f145ad89dd6e692285834b0201c6074a5eeaad3fd4d668c2 |
| distro-1.9.0-py3-none-any.whl | 7bffd925d65168f85027d8da9af6bddab658135b840670a223589bc0c8ef02b2 |
| greenlet-3.5.6-cp312-cp312-win_amd64.whl | 7ac4abb3877c43af320392c664774eef6fa2cc063c79a55fc02d844a3cbe7395 |
| grpcio-1.84.0-cp312-cp312-win_amd64.whl | 49717e857899f4136d7657bf5aded61ac479110a075438290923a4d86af7cd02 |
| h11-0.16.0-py3-none-any.whl | 63cf8bbe7522de3bf65932fda1d9c2772064ffb3dae62d55932da54b31cb6c86 |
| h2-4.4.1-py3-none-any.whl | 0e25f1462b23c9cb82d9eb02e28bc706dac2a68cb457c6a0d74d63c8a2a5d0e6 |
| hpack-4.2.0-py3-none-any.whl | 858ac0b02280fa582b5080d68db0899c62a80375e0e5413a74970c5e518b6986 |
| httpcore-1.0.9-py3-none-any.whl | 2d400746a40668fc9dec9810239072b40b4484b640a8c38fd654a024c7a1bf55 |
| httpcore2-2.13.0-py3-none-any.whl | 35ae5be347aa40467b4a5dc032ac67ebb6d27189fc97e8cebcf99616f6a1bb9e |
| httpx-0.28.1-py3-none-any.whl | d909fcccc110f8c7faf814ca82a9a4d816bc5a6dbfea25d6591d6985b8ba59ad |
| httpx2-2.13.0-py3-none-any.whl | fc12720cedf72faa26cca6b4ca394e05c894e7d7933fc45cafe767960804e49a |
| hyperframe-6.1.0-py3-none-any.whl | b03380493a519fce58ea5af42e4a42317bf9bd425596f7a0835ffce80f1a42e5 |
| idna-3.20-py3-none-any.whl | ab7ae7122974553370f0bdb919e1a960b2cd1bc1ef0276416d896db81c14582c |
| jiter-0.17.0-cp312-cp312-win_amd64.whl | 9bd3caac219df476dd0cc3fe01d2f1581ed588906feac767abd9614c1c12f8b3 |
| mem0ai-2.0.20-py3-none-any.whl | 655103616fe70b67e0434f6725aad35269a071adcd1f61426553b10e0b1b076c |
| numpy-2.5.3-cp312-cp312-win_amd64.whl | 0a59a421a32580a009e8a1751345bf829631b990dc1794b80514ab722b435def |
| openai-3.16.2-py3-none-any.whl | 660a7f8307e6605342ae84ff4425412e7cd19e5e9b298f975502df2a1a9a5d5f |
| portalocker-3.2.0-py3-none-any.whl | 3cdc5f565312224bc570c49337bd21428bba0ef363bbcf58b9ef4a9f11779968 |
| posthog-7.58.0-py3-none-any.whl | 2ac581ce52c63acb3a551c9066652c3d29e1de1ea06e70655ebe8b866e80f334 |
| protobuf-6.33.6-cp310-abi3-win_amd64.whl | 0cd27b587afca21b7cfa59a74dcbd48a50f0a6400cfb59391340ad729d91d326 |
| pydantic-2.13.5-py3-none-any.whl | 346a034f080da3755d8e9cb5e00e8b07de1d39e4f6e2c87d8ab7cafa0b269a73 |
| pydantic_core-2.46.5-cp312-cp312-win_amd64.whl | 5e81740c09e310f5aa5cbd3e434a01c154d4bef93241c7877b39f211d2b78ba8 |
| pytz-2026.3.post1-py2.py3-none-any.whl | dd95840dd199baea12d9cc096a1d452caa6596a1c1e4b5f3dbd1541855d5e815 |
| pywin32-312-cp312-cp312-win_amd64.whl | b457f6d628a47e8a7346ce22acb7e1a46a4a78b52e1d17e1af56871bd19a93bc |
| qdrant_client-1.19.1-py3-none-any.whl | fca1a96c3f90f5fff853f6ee6877838a5768a04c963df9891a655a63313af8a0 |
| requests-2.34.2-py3-none-any.whl | 2a0d60c172f83ac6ab31e4554906c0f3b3588d37b5cb939b1c061f4907e278e0 |
| sniffio-1.3.1-py3-none-any.whl | 2f6da418d1f1e0fddd844478f41680e794e6051915791a034ff65e5f100525a2 |
| sqlalchemy-2.0.54-cp312-cp312-win_amd64.whl | d65f8ca742ef1e1e14bc417ef59dc2ddf207a7b66b30cfdc6152447314e030cf |
| truststore-0.10.4-py3-none-any.whl | adaeaecf1cbb5f4de3b1959b42d41f6fab57b2b1666adb59e89cb0b53361d981 |
| typing_extensions-4.16.0-py3-none-any.whl | 481caa481374e813c1b176ada14e97f1f67a4539ce9cfeb3f350d78d6370c2e8 |
| typing_inspection-0.4.4-py3-none-any.whl | 65b8397ba37ccbce054456aaccddfc91e6e3083c92824df348d96ca832f3f147 |
| urllib3-2.8.0-py3-none-any.whl | 0cf3cae568d36aa9576b28dfb35f11328f1cb974ca7647d9475ebb86c75ac6e3 |

## Explicit remaining limits

No real StudyCrew ProviderRegistry/Gateway, Task Runtime authority, DLP/approval/budget/cancellation, encryption-key ownership, MemoryRepository, repository CAS/generations/outbox, deletion replay protection, sensitive classification, global promotion, migration, production local embedding model, USearch index, PyInstaller build, source lifecycle or UI flow was integrated or certified. History privacy under failures/concurrency and disk residuals outside this bounded Python guard remain release gates. Query embeddings and LLM output are inert fixtures, so there is no recall-quality, ranking-quality, latency or real-provider claim. No expanded fault matrix, full project suite, golden benchmark or security acceptance was run.

The bounded decision is to proceed under the user's existing approved integration plan with the pinned normal-subclass/factory approach; keep encrypted repository authority and separate disposable approved/candidate arenas; implement sticky-failure and authoritative postvalidation before treating SDK results as durable application success. This preflight does not itself certify production integration.
