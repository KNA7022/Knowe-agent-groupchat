"""Local evidence retrieval behind KnoweServer's authenticated HTTP boundary."""
from __future__ import annotations

import asyncio
import base64
import binascii
from collections import OrderedDict
import json
from pathlib import Path
from urllib.parse import parse_qs
import uuid

from backend.groups import GroupDomainError, GroupLifecycle, GroupNotFoundError, GroupRepository
from backend.knowledge_api import KnowledgeHttpResponse
from backend.knowe_rag.bm25 import Bm25Index
from backend.knowe_rag.chunks import CHILD, build_chunks
from backend.knowe_rag.crypto import MasterKey, MasterKeyError
from backend.knowe_rag.parsers import (
    MAX_EXTRACTED_CHARS, MAX_IMPORT_BYTES, MAX_PARSE_BLOCKS, SUPPORTED_SUFFIXES,
    UnsupportedFormat, parse_document_bytes, validate_parsed_output,
)
from backend.knowe_rag.store import EncryptedKnowledgeStore, StoreError


class ScopeDenied(RuntimeError):
    pass


MAX_PUBLICATION_CHUNKS = 20_000
MAX_PUBLICATION_TEXT_CHARS = 2 * MAX_EXTRACTED_CHARS + 2 * MAX_PARSE_BLOCKS


def _prepare_import(raw, suffix, source_id, content_hasher):
    blocks = parse_document_bytes(raw, suffix)
    validate_parsed_output(blocks)
    chunks = build_chunks(source_id, blocks, content_hasher=content_hasher)
    if len(chunks) > MAX_PUBLICATION_CHUNKS or sum(len(chunk.text) for chunk in chunks) > MAX_PUBLICATION_TEXT_CHARS:
        raise UnsupportedFormat("chunk_output_exceeds_import_policy")
    return blocks, chunks


class LocalKnowledgeService:
    def __init__(self, root: Path, groups: GroupRepository, *, master_key: MasterKey | None = None):
        self.groups = groups
        root = Path(root) / "local_knowledge"
        self.store = EncryptedKnowledgeStore(root / "knowledge.sqlite", master_key=master_key or MasterKey.load_or_create(root / "master-key.dpapi"))
        self._cache: OrderedDict[tuple, tuple[Bm25Index, dict]] = OrderedDict()
        self.reconcile()

    def close(self):
        self._cache.clear()
        self.store.close()

    def scope(self, group_id: str, course_id: str | None, *, mutation=False):
        try:
            group = self.groups.get(group_id)
        except GroupNotFoundError as exc:
            raise ScopeDenied("unknown_group") from exc
        transaction = self.groups.delete_transaction(group_id)
        if transaction and transaction.get("stage") != "committed":
            raise ScopeDenied("group_deletion_in_progress")
        if group.lifecycle not in {GroupLifecycle.ACTIVE, GroupLifecycle.ARCHIVED}:
            raise ScopeDenied("group_unavailable")
        if mutation and group.lifecycle != GroupLifecycle.ACTIVE:
            raise ScopeDenied("archived_group_read_only")
        if course_id and course_id not in {binding.course_id for binding in group.course_bindings}:
            raise ScopeDenied("course_outside_group_scope")
        return group

    def reconcile(self):
        """Recover deletion interrupted after canonical group/source commit, retaining shared sources."""
        linked = {link.source_id for group in self.groups.list_groups() for link in group.source_links}
        removed = False
        for source_id in self.store.document_ids():
            if source_id not in linked:
                self.store.delete_document(source_id)
                removed = True
        # Clear on group changes too: no stale decrypted corpus remains accessible in cache.
        self._cache.clear()
        if removed:
            self.store.checkpoint()

    def invalidate(self):
        self._cache.clear()

    def sources(self, group_id, course_id):
        group = self.scope(group_id, course_id)
        rows = []
        for link in group.source_links:
            metadata = self.store.source_metadata(link.source_id)
            if metadata is not None and (not course_id or metadata.get("course_id") == course_id):
                rows.append({"source_id": link.source_id, **metadata})
        return rows

    def require_source(self, group_id, course_id, source_id, *, mutation=False):
        self.scope(group_id, course_id, mutation=mutation)
        row = next((row for row in self.sources(group_id, course_id) if row["source_id"] == source_id), None)
        if row is None:
            raise ScopeDenied("source_outside_group_scope")
        return row

    def corpus(self, group_id, course_id):
        rows = self.sources(group_id, course_id)
        key = (group_id, course_id, tuple(row["source_id"] for row in rows))
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        index, evidence = Bm25Index(), {}
        for row in rows:
            for chunk in self.store.read_chunks(row["source_id"]):
                if chunk.level == CHILD and chunk.text and chunk.kind != "image_page":
                    index.add(chunk.chunk_id, chunk.text)
                    evidence[chunk.chunk_id] = {"source_id": row["source_id"], "filename": row["filename"],
                        "chunk_id": chunk.chunk_id, "location": chunk.location, "text": chunk.text}
        if sum(len(row["text"]) for row in evidence.values()) <= 2_000_000:
            self._cache[key] = (index, evidence)
            while len(self._cache) > 4:
                self._cache.popitem(last=False)
        return index, evidence

    async def dispatch(self, method, path, query, body):
        try:
            params = parse_qs(query, keep_blank_values=True)
            if len(params.get("group_id", [])) != 1 or not params["group_id"][0]:
                raise ScopeDenied("exactly_one_canonical_group_required")
            if len(params.get("course_id", [])) > 1:
                raise ScopeDenied("exactly_one_course_scope_allowed")
            group_id = params["group_id"][0]
            course_id = (params.get("course_id") or [None])[0] or None
            self.scope(group_id, course_id, mutation=method == "POST" and not path.endswith("/search"))
            payload = json.loads(body.decode("utf-8")) if body else {}
            if not isinstance(payload, dict):
                raise ValueError("bad_json")
            route = path.removeprefix("/api/knowledge/local/")
            if route == "sources" and method == "GET":
                result = {"sources": self.sources(group_id, course_id)}
            elif route == "import" and method == "POST":
                filename = payload.get("filename")
                encoded = payload.get("data_base64")
                if (not isinstance(filename, str) or not 0 < len(filename) <= 255
                        or any(char in filename for char in ("/", "\\", "\x00"))
                        or not isinstance(encoded, str) or len(encoded) > (MAX_IMPORT_BYTES + 2) // 3 * 4):
                    raise ValueError("invalid_import")
                suffix = Path(filename).suffix.casefold()
                if suffix not in SUPPORTED_SUFFIXES:
                    raise ValueError("unsupported_document_format")
                raw = base64.b64decode(encoded, validate=True)
                if not raw or len(raw) > MAX_IMPORT_BYTES:
                    raise ValueError("invalid_import_size")
                before = self.scope(group_id, course_id, mutation=True)
                binding = next((b.binding_id for b in before.course_bindings if b.course_id == course_id), None)
                source_id = "src_" + uuid.uuid4().hex
                blocks, chunks = await asyncio.to_thread(_prepare_import, raw, suffix, source_id, self.store.content_hasher)
                current = self.scope(group_id, course_id, mutation=True)
                if binding != next((b.binding_id for b in current.course_bindings if b.course_id == course_id), None):
                    raise ScopeDenied("scope_changed_during_import")
                needs_vision = any(block.kind == "image_page" for block in blocks)
                metadata = {"filename": filename, "course_id": course_id, "byte_count": len(raw),
                    "chunk_count": sum(c.level == CHILD and bool(c.text) for c in chunks),
                    "status": "needs_vision" if needs_vision else "indexed" if chunks else "empty"}
                try:
                    self.store.write_document(source_id, chunks)
                    self.store.write_source(source_id, metadata, raw)
                    self.groups.link_source(group_id, source_id)
                except Exception:
                    self.store.delete_document(source_id)
                    self.store.checkpoint()
                    raise
                self._cache.clear()
                result = {"source": {"source_id": source_id, **metadata}}
            elif route == "search" and method == "POST":
                query_text = payload.get("query")
                if not isinstance(query_text, str) or not 0 < len(query_text.strip()) <= 1000:
                    raise ValueError("invalid_query")
                index, evidence = self.corpus(group_id, course_id)
                matches = await asyncio.to_thread(index.search, query_text, limit=20)
                # Search outlives unlink/delete/course changes. Reauthorize every returned source.
                allowed = {row["source_id"] for row in self.sources(group_id, course_id)}
                result = {"results": [{**evidence[chunk_id], "score": score} for chunk_id, score in matches
                    if evidence[chunk_id]["source_id"] in allowed]}
            elif route == "detail" and method == "GET":
                source_id = (params.get("source_id") or [""])[0]
                row = self.require_source(group_id, course_id, source_id)
                chunk_id = (params.get("chunk_id") or [None])[0]
                chunks = self.store.read_chunks(source_id)
                result = {"source": row, "chunks": [{"chunk_id": c.chunk_id, "location": c.location, "text": c.text, "kind": c.kind}
                    for c in chunks if c.level == CHILD and (not chunk_id or c.chunk_id == chunk_id)]}
            elif route == "delete" and method == "POST":
                source_id = payload.get("source_id")
                self.require_source(group_id, course_id, source_id, mutation=True)
                self.groups.unlink_source(group_id, source_id)
                self.reconcile()
                result = {"deleted": True, "retained_for_other_groups": self.groups.source_reference_count(source_id) > 0}
            else:
                return KnowledgeHttpResponse(404, {"ok": False, "error": "not_found"})
            group = self.scope(group_id, course_id)
            return KnowledgeHttpResponse(200, {"ok": True, "mode": "lexical", "degraded": True, "group": group.to_dict(), **result})
        except ScopeDenied as exc:
            return KnowledgeHttpResponse(403, {"ok": False, "error": str(exc)})
        except (ValueError, UnicodeError, binascii.Error, UnsupportedFormat):
            # Parser exception messages may contain user text; never log or echo them.
            return KnowledgeHttpResponse(400, {"ok": False, "error": "invalid_local_document_or_request"})
        except GroupDomainError:
            return KnowledgeHttpResponse(409, {"ok": False, "error": "group_or_source_state_changed"})
        except (StoreError, MasterKeyError):
            return KnowledgeHttpResponse(503, {"ok": False, "error": "encrypted_storage_unavailable"})
