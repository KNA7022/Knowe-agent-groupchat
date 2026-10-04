# Mem0 adapter preflight — read-only, not implemented

2026-10-04: inspected the already downloaded wheel `D:/agentwork/wheelprobe-mem0/mem0ai-2.0.20-py3-none-any.whl` with .NET ZipFile/UTF-8 readers. No install, import, extraction or network call occurred. User's explicit Mem0 OSS authorization stands; this is integration preparation, not a new product approval request.

Verified pinned-wheel interfaces:

- `mem0/utils/factory.py`: LlmFactory has register_provider(name, class_path, config_class). EmbedderFactory and VectorStoreFactory expose class maps but no equivalent registration method. Vector factory accepts config.model_dump().
- `mem0/vector_stores/configs.py`: VectorStoreConfig validates against a closed provider/config map and dynamically imports mem0.configs.vector_stores.<provider>. Merely adding a factory map entry is insufficient for a normal custom config. An adapter design must explicitly solve this without silently invoking default Qdrant or patching installed dependency files.
- `Memory.__init__` invokes embedding/vector/LLM factories, creates SQLiteManager(config.history_db_path), reads collection_name, and optionally constructs a reranker. Telemetry true creates a separate migrations vector collection. Ensure MEM0_TELEMETRY=False before importing the module; history must be `:memory:`.
- Even with graph memory out of scope, this version has a lazy entity_store that clones the vector config and invokes the same factory for another collection. An ephemeral adapter must keep every such collection in memory and namespace isolated; testing only the main collection does not prove absence of persistence.
- Default MemoryConfig is instantiated at module definition, so import-time setup/environment must be controlled before importing Mem0. Earlier inspection found MEM0_DIR setup/config UUID writes; use a dedicated ephemeral directory containing no user content.
- Some internal exception/debug logs interpolate exceptions. Gateway wrappers must prevent content/credentials entering exceptions and suppress/redact third-party content logs; disabling telemetry alone is insufficient.

The approved encrypted MemoryRepository remains the sole durable authority. Normal recall must not include candidate or tombstone namespaces. All extraction calls must use actual governed Provider Registry/Gateway authority propagated across any Mem0 thread boundaries, never raw provider keys. A future implementation must demonstrate real pinned Mem0 extraction/dedup/recall with inert lowest-level transport, namespace filtering and postvalidation, restart/deletion scans, and no SDK egress outside the Gateway. This file does not prescribe unsafe model_construct bypasses or a home-grown replacement branded Mem0.
