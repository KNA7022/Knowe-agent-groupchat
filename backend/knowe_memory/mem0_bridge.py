"""Controlled bootstrap and disposable real Mem0 projections.

Callers must supply governed model callbacks; no dummy production embeddings,
SDK API keys, cloud defaults, telemetry or persistent SDK history are permitted.
"""
from __future__ import annotations
import importlib.metadata
import importlib.util
import json
import logging
import os
import sys
import tempfile
import threading

PIN = '2.0.20'
_lock = threading.RLock()
_runtime = None
_ready = False


class SemanticMemoryUnavailable(RuntimeError):
    pass


def availability():
    try:
        version = importlib.metadata.version('mem0ai')
    except importlib.metadata.PackageNotFoundError:
        return {'available': False, 'reason': 'mem0_sdk_missing', 'required_version': PIN}
    if version != PIN:
        return {'available': False, 'reason': 'mem0_sdk_version_mismatch', 'required_version': PIN}
    return {'available': False, 'reason': 'governed_local_embedding_not_configured', 'required_version': PIN}


def local_factory():
    """Portable local semantic composition; extraction must receive an approved callback."""
    from .local_embedding import LocalE5Embedding
    embedding = LocalE5Embedding()
    def factory(*, gid, cid, sensitivity, operation, generate=None):
        try:
            embedding.load()
        except (RuntimeError, ImportError):
            raise SemanticMemoryUnavailable('local_embedding_pack_or_runtime_unavailable') from None
        def denied(messages):
            raise SemanticMemoryUnavailable('memory_extraction_requires_provider_approval')
        return Mem0Projection(embed=lambda text, memory_action=None: embedding.embed(text,query=memory_action=='search'),
            generate=generate or denied, model_identity=embedding.model_identity, dimensions=embedding.dimensions)
    return factory


def bootstrap():
    global _runtime, _ready
    with _lock:
        if _ready:
            return
        if 'mem0' in sys.modules:
            raise SemanticMemoryUnavailable('mem0_imported_before_controlled_bootstrap')
        status = availability()
        if status['reason'] != 'governed_local_embedding_not_configured':
            raise SemanticMemoryUnavailable(status['reason'])
        # Installed spaCy can auto-download its model from the SDK's import path.
        if importlib.util.find_spec('spacy') and not importlib.util.find_spec('en_core_web_sm'):
            raise SemanticMemoryUnavailable('spacy_model_missing_download_denied')
        _runtime = tempfile.TemporaryDirectory(prefix='studycrew-mem0-')
        os.environ.update(MEM0_TELEMETRY='False', MEM0_TELEMETRY_SAMPLE_RATE='0', MEM0_DIR=_runtime.name)
        # The SDK logs model-derived content on errors; keep it out of app logs.
        logger = logging.getLogger('mem0')
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        from . import mem0_adapters
        mem0_adapters.register()
        _ready = True


class Mem0Projection:
    """One physical namespace per operation; SDK IDs never confer authority."""
    def __init__(self, *, embed, generate, model_identity, dimensions):
        if not callable(embed) or not callable(generate) or not model_identity:
            raise SemanticMemoryUnavailable('governed_callbacks_required')
        bootstrap()
        from .mem0_adapters import make
        self.sdk, self.arena = make(embed=embed, generate=generate, model=model_identity, dimensions=dimensions)
        self.mapping = {}

    def extract(self, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 16000:
            raise ValueError('invalid_memory_evidence')
        result = self.sdk.add(text, user_id=self.arena.handle, infer=True)
        self.arena.check()
        facts = []
        for item in result.get('results', []):
            stored = self.sdk.get(item.get('id'))
            # SDK may report ADD after swallowing vector insertion failure.
            if item.get('event') != 'ADD' or not stored or stored.get('memory') != item.get('memory'):
                raise SemanticMemoryUnavailable('mem0_result_not_stored')
            facts.append(item['memory'])
        if len(facts) > 100:
            raise ValueError('too_many_memory_facts')
        return facts

    def rebuild(self, records):
        if self.mapping:
            raise SemanticMemoryUnavailable('rebuild_requires_fresh_projection')
        for record in records:
            result = self.sdk.add(record.text, user_id=self.arena.handle, infer=False)
            self.arena.check()
            items = result.get('results', [])
            if len(items) != 1:
                raise SemanticMemoryUnavailable('mem0_projection_failed')
            item = items[0]
            stored = self.sdk.get(item.get('id'))
            if not stored or stored.get('memory') != record.text:
                raise SemanticMemoryUnavailable('mem0_result_not_stored')
            self.mapping[item['id']] = (record.memory_id, record.version)

    def search(self, query, *, resolve, limit=10):
        if not isinstance(query, str) or not query.strip() or len(query) > 2000 or not 1 <= limit <= 50:
            raise ValueError('invalid_memory_query')
        result = self.sdk.search(query, filters={'user_id': self.arena.handle}, top_k=limit, threshold=0, rerank=False)
        self.arena.check()
        records = []
        for item in result.get('results', []):
            authority = self.mapping.get(item.get('id'))
            if authority is None:
                continue
            record = resolve(*authority)  # caller rechecks scope/status/TTL/source/version
            if record is not None and record.text == item.get('memory'):
                records.append(record)
        return records

    def close(self):
        self.mapping.clear()
        self.sdk.close()
        client = getattr(self.sdk.embedding_model.config, 'http_client', None)
        if client is not None:
            client.close()
        self.arena.dispose()

    def __enter__(self): return self
    def __exit__(self, *args): self.close()
