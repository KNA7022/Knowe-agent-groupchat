"""Local evidence retrieval behind KnoweServer's authenticated HTTP boundary."""
from __future__ import annotations

import asyncio
import base64
import binascii
from contextlib import contextmanager
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
    UnsupportedFormat, ParsedBlock, parse_document_bytes, validate_parsed_output,
)
from backend.knowe_rag.office_rendering import OFFICE_SUFFIXES, office_rendering_available, convert_office_to_pdf, OfficeRenderingUnavailable
from backend.knowe_rag.store import EncryptedKnowledgeStore, StoreError
from backend.rag_runtime import RagApprovalRequired
from backend.commercial_gateway import GatewayDenied
from backend.knowe_core.errors import ProviderError


class ScopeDenied(RuntimeError):
    pass


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}

MAX_PUBLICATION_CHUNKS = 20_000
MAX_PUBLICATION_TEXT_CHARS = 2 * MAX_EXTRACTED_CHARS + 2 * MAX_PARSE_BLOCKS


def _prepare_import(raw, suffix, source_id, content_hasher):
    if suffix in IMAGE_SUFFIXES:
        if not ((suffix == ".png" and raw.startswith(b"\x89PNG\r\n\x1a\n")) or
                (suffix in {".jpg", ".jpeg"} and raw.startswith(b"\xff\xd8\xff"))):
            raise UnsupportedFormat("invalid_image")
    blocks = (ParsedBlock("", "page:1", "image_page"),) if suffix in IMAGE_SUFFIXES else parse_document_bytes(raw, suffix)
    validate_parsed_output(blocks)
    chunks = build_chunks(source_id, blocks, content_hasher=content_hasher)
    if len(chunks) > MAX_PUBLICATION_CHUNKS or sum(len(chunk.text) for chunk in chunks) > MAX_PUBLICATION_TEXT_CHARS:
        raise UnsupportedFormat("chunk_output_exceeds_import_policy")
    return blocks, chunks


class LocalKnowledgeCardSourceGuard:
    """Synchronous source snapshot fence for the accepted protected-card machinery.

    Source metadata/version/chunk publication shares the SQLite connection RLock.
    Callers acquire runtime -> canonical group -> this guard, as BoundCardService
    requires. No renderer claim can install this capability.
    """
    def __init__(self, knowledge):
        self.knowledge = knowledge

    @contextmanager
    def guard(self, group):
        from backend.task_runtime.bound_cards import CardSourceRevision, CardScopeRevoked
        with self.knowledge.store._database._lock:
            self.knowledge.scope(group.group_id, None)
            revisions = []
            for link in group.source_links:
                metadata = self.knowledge.store.source_metadata(link.source_id)
                if metadata is None:
                    raise CardScopeRevoked("source revision unavailable")
                revisions.append([link.source_id, metadata.get("version", 1), metadata.get("course_id")])
            revision = self.knowledge.store.content_hasher(json.dumps(revisions, sort_keys=True))
            yield CardSourceRevision(revision)

    def evidence(self, group_id, course_id, query, *, limit=5):
        """Backend-only read tool. Every returned item carries source version."""
        with self.knowledge.store._database._lock:
            index, corpus = self.knowledge.corpus(group_id, course_id)
            hits = index.search(query, limit=min(max(int(limit), 1), 20))
            return [dict(corpus[chunk]) for chunk, _ in hits]


class LocalKnowledgeService:
    def __init__(self, root: Path, groups: GroupRepository, *, master_key: MasterKey | None = None, vision_reader=None, answer_reader=None):
        self.vision_reader = vision_reader
        self.answer_reader = answer_reader
        self._vision_lock = asyncio.Lock()
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
        for operation_id in self.store.operation_ids():
            operation = self.store.operation(operation_id)
            try:
                self.scope(operation["group_id"], operation.get("course_id"))
                for pin in operation["pins"]:
                    row = self.require_source(operation["group_id"], operation.get("course_id"), pin["source_id"])
                    if row.get("version", 1) != pin["version"]:
                        raise ScopeDenied("source_version_changed")
            except ScopeDenied:
                self.store.delete_operation(operation_id)
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
                rows.append({"source_id": link.source_id, **{k: v for k, v in metadata.items() if k != "vision_text"}})
        return rows

    def require_source(self, group_id, course_id, source_id, *, mutation=False):
        self.scope(group_id, course_id, mutation=mutation)
        row = next((row for row in self.sources(group_id, course_id) if row["source_id"] == source_id), None)
        if row is None:
            raise ScopeDenied("source_outside_group_scope")
        return row

    def corpus(self, group_id, course_id):
        rows = self.sources(group_id, course_id)
        key = (group_id, course_id, tuple((row["source_id"], row.get("version", 1)) for row in rows))
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        index, evidence = Bm25Index(), {}
        for row in rows:
            for chunk in self.store.read_chunks(row["source_id"]):
                if chunk.level == CHILD and chunk.text and chunk.kind != "image_page":
                    index.add(chunk.chunk_id, chunk.text)
                    evidence[chunk.chunk_id] = {"source_id": row["source_id"], "filename": row["filename"],
                        "chunk_id": chunk.chunk_id, "version": row.get("version", 1), "location": chunk.location, "text": chunk.text}
        if sum(len(row["text"]) for row in evidence.values()) <= 2_000_000:
            self._cache[key] = (index, evidence)
            while len(self._cache) > 4:
                self._cache.popitem(last=False)
        return index, evidence

    async def process_vision(self, group_id, course_id, payload):
        """Explicit, bounded egress; every completed page is checkpointed encrypted."""
        if payload.get("approved") is not True:
            raise ScopeDenied("vision_requires_explicit_approval")
        if self.vision_reader is None:
            raise ScopeDenied("vision_provider_not_configured")
        limit = payload.get("page_limit", 1)
        if type(limit) is not int or not 1 <= limit <= 4:
            raise ValueError("invalid_page_limit")
        source_id = payload.get("source_id")
        async with self._vision_lock:
            row = self.require_source(group_id, course_id, source_id, mutation=True)
            if type(payload.get("source_version")) is not int or payload["source_version"] != row.get("version", 1):
                raise ScopeDenied("source_version_changed")
            raw = self.store.read_original(source_id)
            suffix = Path(row["filename"]).suffix.casefold()
            blocks, _ = await asyncio.to_thread(_prepare_import, raw, suffix, source_id, self.store.content_hasher)
            render_raw = raw
            office_visual = suffix in OFFICE_SUFFIXES
            if office_visual:
                render_raw = await asyncio.to_thread(convert_office_to_pdf, raw, suffix)
                visual_blocks, _ = await asyncio.to_thread(_prepare_import, render_raw, ".pdf", source_id, self.store.content_hasher)
                blocks = tuple(blocks) + tuple(visual_blocks)
            elif suffix not in IMAGE_SUFFIXES | {".pdf"}:
                raise ScopeDenied("visual_format_not_supported")
            completed = dict((self.store.source_metadata(source_id) or {}).get("vision_text", {}))
            targets = [block.location for block in blocks if block.kind == "image_page"]
            # Include text PDF pages on explicit request, to interpret charts as well.
            if office_visual or payload.get("include_text_pages") is True and suffix == ".pdf":
                targets = list(dict.fromkeys(block.location for block in blocks if block.location.startswith("page:")))
            pending = [location for location in targets if location not in completed]
            for location in pending[:limit]:
                self.require_source(group_id, course_id, source_id, mutation=True)
                if suffix == ".pdf" or office_visual:
                    from backend.knowe_rag.rendering import render_page
                    from backend.knowe_rag.parsers import _PDF_IMPORT_LOCK
                    def render():
                        with _PDF_IMPORT_LOCK:
                            return render_page(render_raw, int(location.split(":")[1])).image
                    image = await asyncio.to_thread(render)
                    mime = "image/png"
                elif suffix in IMAGE_SUFFIXES:
                    image, mime = raw, "image/png" if suffix == ".png" else "image/jpeg"
                else:
                    raise ValueError("visual_format_not_supported")
                version = row.get("version", 1)
                text = await self.vision_reader(image=image, mime_type=mime, location=location,
                    max_output_tokens=2048, authorization={**(payload.get("authorization") or {}),
                        "group_id": group_id, "course_id": course_id, "source_id": source_id,
                        "source_version": version, "location": location, "page_limit": 1})
                if not isinstance(text, str) or not text.strip() or len(text) > 16000:
                    raise ValueError("invalid_vision_output")
                current = self.require_source(group_id, course_id, source_id, mutation=True)
                if current.get("version", 1) != version:
                    raise ScopeDenied("source_version_changed")
                completed[location] = text
                merged = [ParsedBlock((b.text + "\n" + completed[b.location]).strip() if b.location in completed else b.text, b.location,
                    "vision" if b.location in completed else b.kind) for b in blocks]
                validate_parsed_output(merged)
                chunks = build_chunks(source_id, merged, content_hasher=self.store.content_hasher)
                if len(chunks) > MAX_PUBLICATION_CHUNKS or sum(len(chunk.text) for chunk in chunks) > MAX_PUBLICATION_TEXT_CHARS:
                    raise UnsupportedFormat("chunk_output_exceeds_import_policy")
                metadata = {k: v for k, v in row.items() if k != "source_id"}
                metadata.update(vision_text=completed, vision_completed=list(completed),
                    version=row.get("version", 1) + 1,
                    status="indexed" if all(t in completed for t in targets) else "needs_vision",
                    chunk_count=sum(c.level == CHILD and bool(c.text) for c in chunks))
                self.store.write_document(source_id, chunks, metadata=metadata, expected_version=version)
                row = {"source_id": source_id, **metadata}
                self.invalidate()
                if hasattr(self, "rag_runtime") and (payload.get("authorization") or {}).get("operation_id"):
                    self.rag_runtime.finish({**payload["authorization"], "group_id": group_id}, valid=True,
                        citations=({"source_id": source_id, "version": metadata["version"], "location": location},))
                    break  # Each exact provider grant authorizes one page only.
            # Do not send a duplicate copy of full page contents to the renderer.
            public = {k: v for k, v in row.items() if k != "vision_text"}
            return {"source": public, "remaining_pages": sum(t not in completed for t in targets)}

    async def answer(self, group_id, course_id, payload):
        """Return scoped verbatim evidence, or verify a structured model answer.

        Citation validation proves exact quotation membership, not semantic entailment.
        Source/chunk/version identity prevents two documents with page:1 from colliding.
        """
        query = payload.get("query")
        if not isinstance(query, str) or not 0 < len(query.strip()) <= 1000:
            raise ValueError("invalid_query")
        index, corpus = self.corpus(group_id, course_id)
        matches = await asyncio.to_thread(index.search, query, limit=5)
        evidence = [dict(corpus[chunk]) for chunk, _ in matches]
        if not evidence:
            return {"answer": "资料证据不足，请补充资料或调整问题。", "citations": [],
                "verification": "abstain_insufficient", "answer_mode": "local_extract"}
        if self.answer_reader is None or payload.get("use_model") is not True:
            candidate = {"answer": "\n\n".join(item["text"] for item in evidence),
                "citations": [{k: item[k] for k in ("source_id", "chunk_id", "version", "location")} | {"quote": item["text"]} for item in evidence]}
            mode = "local_extract"
        else:
            if payload.get("approved") is not True:
                raise ScopeDenied("answer_requires_explicit_approval")
            candidate = await self.answer_reader(query=query, evidence=evidence,
                authorization={**(payload.get("authorization") or {}), "group_id": group_id, "course_id": course_id}, max_output_tokens=2048)
            mode = "model"
        if not isinstance(candidate, dict) or not isinstance(candidate.get("answer"), str) or len(candidate["answer"]) > 16000:
            raise ValueError("invalid_answer_output")
        citations = candidate.get("citations")
        valid = isinstance(citations, list) and 0 < len(citations) <= 20
        known = {(e["source_id"], e["chunk_id"], e["version"], e["location"]): e for e in evidence}
        for cited in citations if isinstance(citations, list) else []:
            if not isinstance(cited, dict):
                valid = False
                continue
            item = known.get(tuple(cited.get(k) for k in ("source_id", "chunk_id", "version", "location")))
            quote = cited.get("quote")
            if item is None or not isinstance(quote, str) or not quote.strip() or quote not in item["text"]:
                valid = False
                continue
            current = self.require_source(group_id, course_id, item["source_id"])
            if current.get("version", 1) != item["version"]:
                raise ScopeDenied("source_version_changed")
        if hasattr(self, "rag_runtime") and mode == "model" and (payload.get("authorization") or {}).get("operation_id"):
            self.rag_runtime.finish({**payload["authorization"], "group_id": group_id}, valid=valid, citations=citations if valid else ())
        if not valid:
            return {"answer": "答案引用未通过原文核验，请重新提问。", "citations": [],
                "verification": "abstain_unsupported_citation", "answer_mode": mode}
        return {"answer": candidate["answer"], "citations": citations,
            "verification": "exact_quotes_verified", "answer_mode": mode,
            "semantic_entailment_verified": False}

    async def dispatch(self, method, path, query, body):
        try:
            params = parse_qs(query, keep_blank_values=True)
            if len(params.get("group_id", [])) != 1 or not params["group_id"][0]:
                raise ScopeDenied("exactly_one_canonical_group_required")
            if len(params.get("course_id", [])) > 1:
                raise ScopeDenied("exactly_one_course_scope_allowed")
            group_id = params["group_id"][0]
            course_id = (params.get("course_id") or [None])[0] or None
            self.scope(group_id, course_id, mutation=method == "POST" and not path.endswith(("/search", "/answer")))
            payload = json.loads(body.decode("utf-8")) if body else {}
            if not isinstance(payload, dict):
                raise ValueError("bad_json")
            route = path.removeprefix("/api/knowledge/local/")
            if route == "sources" and method == "GET":
                result = {"sources": self.sources(group_id, course_id),
                    "pending_operations": self.rag_runtime.pending(group_id, course_id) if hasattr(self, "rag_runtime") else []}
            elif route == "import" and method == "POST":
                filename = payload.get("filename")
                encoded = payload.get("data_base64")
                if (not isinstance(filename, str) or not 0 < len(filename) <= 255
                        or any(char in filename for char in ("/", "\\", "\x00"))
                        or not isinstance(encoded, str) or len(encoded) > (MAX_IMPORT_BYTES + 2) // 3 * 4):
                    raise ValueError("invalid_import")
                suffix = Path(filename).suffix.casefold()
                if suffix not in SUPPORTED_SUFFIXES | IMAGE_SUFFIXES:
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
                    "version": 1, "vision_completed": [],
                    "visual_support": "supported" if suffix in IMAGE_SUFFIXES | {".pdf"} else "native_office_available" if office_rendering_available(suffix) else "unsupported_office_visuals" if suffix in OFFICE_SUFFIXES else "text_only",
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
            elif route == "vision" and method == "POST":
                result = await self.process_vision(group_id, course_id, payload)
            elif route == "answer" and method == "POST":
                result = await self.answer(group_id, course_id, payload)
            elif route == "search" and method == "POST":
                query_text = payload.get("query")
                if not isinstance(query_text, str) or not 0 < len(query_text.strip()) <= 1000:
                    raise ValueError("invalid_query")
                index, evidence = self.corpus(group_id, course_id)
                matches = await asyncio.to_thread(index.search, query_text, limit=20)
                # Search outlives unlink/delete/course changes. Reauthorize every returned source.
                allowed = {(row["source_id"], row.get("version", 1)) for row in self.sources(group_id, course_id)}
                result = {"results": [{**evidence[chunk_id], "score": score} for chunk_id, score in matches
                    if (evidence[chunk_id]["source_id"], evidence[chunk_id]["version"]) in allowed]}
            elif route == "detail" and method == "GET":
                source_id = (params.get("source_id") or [""])[0]
                row = self.require_source(group_id, course_id, source_id)
                expected_version = (params.get("source_version") or [None])[0]
                if expected_version is not None and expected_version != str(row.get("version", 1)):
                    raise ScopeDenied("source_version_changed")
                chunk_id = (params.get("chunk_id") or [None])[0]
                chunks = self.store.read_chunks(source_id)
                result = {"source": {k: v for k, v in row.items() if k != "vision_text"}, "chunks": [{"chunk_id": c.chunk_id, "location": c.location, "text": c.text, "kind": c.kind}
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
        except RagApprovalRequired as exc:
            return KnowledgeHttpResponse(202, {"ok": True, "approval_required": True, **exc.public})
        except GatewayDenied:
            return KnowledgeHttpResponse(403, {"ok": False, "error": "rag_authority_or_model_unavailable"})
        except ScopeDenied as exc:
            return KnowledgeHttpResponse(403, {"ok": False, "error": str(exc)})
        except OfficeRenderingUnavailable as exc:
            return KnowledgeHttpResponse(503, {"ok": False, "error": str(exc), "visual_status": "unavailable", "text_index_retained": True})
        except (ValueError, UnicodeError, binascii.Error, UnsupportedFormat):
            # Parser exception messages may contain user text; never log or echo them.
            return KnowledgeHttpResponse(400, {"ok": False, "error": "invalid_local_document_or_request"})
        except GroupDomainError:
            return KnowledgeHttpResponse(409, {"ok": False, "error": "group_or_source_state_changed"})
        except ProviderError:
            return KnowledgeHttpResponse(503, {"ok": False, "error": "rag_provider_unavailable_fresh_approval_required"})
        except (StoreError, MasterKeyError):
            return KnowledgeHttpResponse(503, {"ok": False, "error": "encrypted_storage_unavailable"})
