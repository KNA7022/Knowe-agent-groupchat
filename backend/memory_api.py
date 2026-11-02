"""Governed memory with authoritative local records and approved model extraction.

The accepted encrypted repository owns records, candidates, TTL and tombstones.
This adapter checks current course/source ownership before every presentation and
transition. Versioned evidence is encrypted alongside the authoritative records;
legacy records without evidence are quarantined rather than silently recalled.
"""
from __future__ import annotations

import json
from contextlib import contextmanager, ExitStack
import threading
from pathlib import Path
from urllib.parse import parse_qs

from backend.groups import GroupLifecycle, GroupNotFoundError
from backend.knowledge_api import KnowledgeHttpResponse
from backend.knowe_rag.crypto import field_aad
from backend.knowe_memory.contracts import MemorySensitivity, MemoryStatus
from backend.knowe_memory.repository import MemoryRepository, MemoryRepositoryError
from backend.knowe_memory.mem0_bridge import availability, SemanticMemoryUnavailable


class MemoryService:
    def __init__(self, root, groups, master_key, *, learning, knowledge=None, semantic_factory=None, runtime=None):
        self.groups, self.learning, self.knowledge = groups, learning, knowledge
        self.key = master_key
        self.runtime = runtime
        self.semantic_factory = semantic_factory
        self.semantic_ready = False
        self.semantic_failure = None
        self.repository = MemoryRepository(Path(root) / 'memory' / 'memory.sqlite', master_key=master_key)
        self.repository._database.initialize('CREATE TABLE IF NOT EXISTS memory_evidence (memory_id TEXT PRIMARY KEY, sealed BLOB NOT NULL);')
        self.lock = threading.RLock()
        self.extractions = None
        if runtime is not None:
            from .knowe_memory.extraction_runtime import MemoryExtractionRuntime
            self.extractions = MemoryExtractionRuntime(self, runtime)
        self.reconcile()

    @contextmanager
    def authority_guard(self):
        """Fence current authority through commits; never hold this across await."""
        with ExitStack() as stack:
            if self.runtime is not None:
                stack.enter_context(self.runtime._lock)
            admission = getattr(self.groups, 'admission_guard', None)
            if admission is not None:
                stack.enter_context(admission())
            stack.enter_context(self.learning.lock)
            if self.knowledge is not None:
                stack.enter_context(self.knowledge.store._database._lock)
            stack.enter_context(self.lock)
            yield

    def close(self):
        self.repository.close()

    def scope(self, gid, cid, mutation=False):
        self.learning.scope(gid, mutation)
        group = self.groups.get(gid)
        courses = {row['id'] for row in self.learning.read(gid)['courses']}
        courses.update(binding.course_id for binding in getattr(group, 'course_bindings', []))
        if not cid or cid not in courses:
            raise PermissionError('course_outside_group_scope')

    def evidence(self, record):
        with self.repository._database.transaction(immediate=False) as connection:
            row = connection.execute('SELECT sealed FROM memory_evidence WHERE memory_id=?', (record.memory_id,)).fetchone()
        if not row:
            raise PermissionError('memory_evidence_unavailable')
        return json.loads(self.key.open(row['sealed'], aad=field_aad(record.memory_id, 'memory.evidence')))

    def source(self, gid, cid, evidence):
        self.scope(gid, cid)
        if evidence.get('kind') == 'manual':
            if evidence.get('version') != 1:
                raise PermissionError('source_version_changed')
            return
        if evidence.get('kind') != 'knowledge' or self.knowledge is None:
            raise PermissionError('source_unavailable')
        # Learning courses are group-local; RAG courses also require a canonical binding.
        row = self.knowledge.require_source(gid, cid, evidence.get('source_id'))
        if row.get('version', 1) != evidence.get('version'):
            raise PermissionError('source_version_changed')

    def current(self, record, gid, cid):
        if record.scope.group_id != gid or record.scope.course_id != cid:
            raise PermissionError('memory_outside_scope')
        evidence = self.evidence(record)
        self.source(gid, cid, evidence)
        return evidence

    def reconcile(self):
        """Replay lifecycle cleanup; no decrypted recall cache or semantic index exists."""
        with self.authority_guard():
            # Deleted memories may be represented only by tombstones. A later
            # course/group deletion must remove those encrypted scope remnants.
            with self.repository._database.transaction(immediate=False) as connection:
                tombstones=connection.execute('SELECT memory_id,scope_blob FROM memory_tombstones').fetchall()
            for row in tombstones:
                gid,cid=json.loads(self.key.open(row['scope_blob'],aad=field_aad(row['memory_id'],'memory.tombstone.scope')))
                try:self.scope(gid,cid)
                except (PermissionError,GroupNotFoundError):
                    self.repository.cascade_delete(group_id=gid,course_id=cid)
            with self.repository._database.transaction(immediate=False) as connection:
                ids = [row['memory_id'] for row in connection.execute('SELECT memory_id FROM memory_records')]
            for memory_id in ids:
                record = self.repository.record(memory_id=memory_id)
                if record is None:
                    continue
                try:
                    group = self.groups.get(record.scope.group_id)
                    if group.lifecycle not in {GroupLifecycle.ACTIVE, GroupLifecycle.ARCHIVED}:
                        raise PermissionError()
                except (PermissionError, GroupNotFoundError):
                    self.repository.cascade_delete(group_id=record.scope.group_id)
                    continue
                try:
                    self.scope(record.scope.group_id, record.scope.course_id)
                except PermissionError:
                    self.repository.cascade_delete(group_id=record.scope.group_id, course_id=record.scope.course_id)
                    continue
                try:
                    self.current(record, record.scope.group_id, record.scope.course_id)
                except (PermissionError, RuntimeError, ValueError):
                    # Retain a tombstone when evidence is invalidated, blocking relearning.
                    self.repository.delete(memory_id=memory_id)
            with self.repository._database.transaction() as connection:
                connection.execute('DELETE FROM memory_evidence WHERE memory_id NOT IN (SELECT memory_id FROM memory_records)')
            self.repository.sweep_expired()
            with self.repository._database.transaction() as connection:
                connection.execute('DELETE FROM memory_evidence WHERE memory_id NOT IN (SELECT memory_id FROM memory_records)')
            self.repository.checkpoint()
            if self.extractions is not None:
                self.extractions.reconcile()

    def view(self, gid, cid, sensitivity):
        self.scope(gid, cid)
        def render(record):
            try:
                evidence = self.current(record, gid, cid)
            except (PermissionError, RuntimeError, ValueError):
                return None
            return {'id': record.memory_id, 'text': record.text, 'version': record.version,
                    'status': record.scope.status.value, 'sensitivity': record.scope.sensitivity.value,
                    'expires_at': record.expires_at, 'source': evidence}
        active = self.repository.recall(group_id=gid, course_id=cid, sensitivity=sensitivity, limit=200)
        candidates = self.repository.candidates(group_id=gid, course_id=cid)
        return {'ok': True, 'mode': 'local_exact', 'semantic_recall': False,
                'semantic_memory': self.semantic_status(),
                'records': [row for item in active if (row := render(item))],
                'candidates': [row for item in candidates if (row := render(item))]}

    def semantic_status(self):
        if self.semantic_factory is None:
            return {**availability(), 'configured': False, 'status': 'unavailable'}
        return {'available': self.semantic_ready, 'configured': True,
                'status': 'ready' if self.semantic_ready else ('unavailable' if self.semantic_failure else 'configured_unverified'),
                'reason': self.semantic_failure, 'required_version': '2.0.20'}

    def semantic_verified(self):
        self.semantic_ready = True
        self.semantic_failure = None

    def commit_facts(self, gid, cid, sensitivity, source, facts):
        with self.authority_guard():
            self.scope(gid,cid,True);self.source(gid,cid,source)
            for fact in facts:
                outcome=self.repository.write(text=self.text(fact),group_id=gid,course_id=cid,sensitivity=sensitivity,
                    inferred=True,source_document_id=source.get('source_id','manual'),ttl_seconds=90*86400)
                if outcome.created:
                    blob=self.key.seal(json.dumps(source).encode('utf-8'),aad=field_aad(outcome.stored.memory_id,'memory.evidence'))
                    with self.repository._database.transaction() as connection:
                        connection.execute('INSERT INTO memory_evidence VALUES (?,?)',(outcome.stored.memory_id,blob))

    async def dispatch(self, method, path, query, body):
        try:
            params = parse_qs(query)
            gid, cid = (params.get('group_id') or [''])[0], (params.get('course_id') or [''])[0]
            sensitivity = MemorySensitivity((params.get('sensitivity') or ['public'])[0])
            if method == 'POST' and path == '/api/memory/action':
                proposal = json.loads(body)
                if isinstance(proposal,dict) and proposal.get('action') in {'extract','cancel_extraction'} and self.extractions is not None:
                    if proposal['action']=='extract':
                        prior = self.extractions.read(proposal.get('operation_id')) if proposal.get('operation_id') else None
                        await self.extractions.extract(gid,cid,sensitivity,proposal)
                        if prior is None or prior.get("state") != "completed":
                            self.semantic_verified()
                    else:
                        self.extractions.cancel(gid,cid,proposal.get('operation_id'))
                    result=self.view(gid,cid,sensitivity)
                    result['pending_operations']=self.extractions.pending(gid,cid)
                    return KnowledgeHttpResponse(200,result)
            with self.authority_guard():
                self.scope(gid, cid, method == 'POST')
                if method == 'GET' and path == '/api/memory/state':
                    result=self.view(gid,cid,sensitivity)
                    result['pending_operations']=self.extractions.pending(gid,cid) if self.extractions else []
                    return KnowledgeHttpResponse(200, result)
                if method != 'POST' or path != '/api/memory/action':
                    return KnowledgeHttpResponse(404, {'ok': False, 'error': 'not_found'})
                payload = json.loads(body)
                if not isinstance(payload, dict):
                    raise ValueError('invalid_payload')
                action = payload.get('action')
                if action in {'extract', 'search'}:
                    if self.semantic_factory is None:
                        return KnowledgeHttpResponse(503, {'ok': False, 'error': availability()['reason'], 'semantic_memory': availability()})
                    # The factory must bind current scope, operation and local model identity;
                    # records/candidates use physically separate short-lived SDK instances.
                    with self.semantic_factory(gid=gid, cid=cid, sensitivity=sensitivity.value, operation=action) as projection:
                        if action == 'search':
                            records = self.repository.recall(group_id=gid, course_id=cid, sensitivity=sensitivity)
                            records = [r for r in records if self.current(r, gid, cid)]
                            projection.rebuild(records)
                            def resolve(mid, version):
                                r = self.repository.record(memory_id=mid)
                                if r is None or r.version != version or r.scope.status is not MemoryStatus.ACTIVE:
                                    return None
                                self.current(r, gid, cid)
                                allowed = self.repository.recall(group_id=gid, course_id=cid, sensitivity=sensitivity)
                                return r if r.memory_id in {x.memory_id for x in allowed} else None
                            found = projection.search(self.text(payload.get('query')), resolve=resolve)
                            self.semantic_verified()
                            result = self.view(gid, cid, sensitivity)
                            result.update(mode='mem0_semantic', semantic_recall=True, records=[r for r in result['records'] if r['id'] in {x.memory_id for x in found}])
                            return KnowledgeHttpResponse(200, result)
                        source = payload.get('source', {'kind':'manual', 'version':1})
                        if not isinstance(source, dict):
                            raise ValueError('invalid_source')
                        self.source(gid, cid, source)
                        facts = projection.extract(self.text(payload.get('text')))
                        self.source(gid, cid, source)  # evidence may change during SDK work
                        for fact in facts:
                            self.scope(gid, cid, True)
                            outcome = self.repository.write(text=self.text(fact), group_id=gid, course_id=cid,
                                sensitivity=sensitivity, inferred=True, source_document_id=source.get('source_id','manual'), ttl_seconds=90*86400)
                            if outcome.created:
                                blob=self.key.seal(json.dumps(source).encode('utf-8'), aad=field_aad(outcome.stored.memory_id,'memory.evidence'))
                                with self.repository._database.transaction() as connection:
                                    connection.execute('INSERT INTO memory_evidence VALUES (?,?)',(outcome.stored.memory_id,blob))
                    self.semantic_verified()
                    return KnowledgeHttpResponse(200, self.view(gid,cid,sensitivity))
                elif action == 'write':
                    text = self.text(payload.get('text'))
                    if type(payload.get('inferred')) is not bool:
                        raise ValueError('inferred_flag_required')
                    evidence = payload.get('source', {'kind': 'manual', 'version': 1})
                    if not isinstance(evidence, dict) or type(evidence.get('version')) is not int:
                        raise ValueError('source_version_required')
                    self.source(gid, cid, evidence)
                    ttl = payload.get('ttl_seconds', 90 * 86400)
                    if type(ttl) is not int or not 60 <= ttl <= 365 * 86400:
                        raise ValueError('invalid_ttl')
                    outcome = self.repository.write(text=text, group_id=gid, course_id=cid,
                        sensitivity=MemorySensitivity(payload.get('sensitivity', 'personal')),
                        inferred=payload['inferred'], source_document_id=evidence.get('source_id', 'manual'), ttl_seconds=ttl)
                    if outcome.blocked:
                        return KnowledgeHttpResponse(409, {'ok': False, 'error': 'deleted_memory_cannot_be_relearned'})
                    record = outcome.stored
                    # Deduplication must preserve original evidence, never rebind stale records.
                    if outcome.created:
                        blob = self.key.seal(json.dumps(evidence).encode('utf-8'), aad=field_aad(record.memory_id, 'memory.evidence'))
                        with self.repository._database.transaction() as connection:
                            connection.execute('INSERT INTO memory_evidence VALUES (?,?)', (record.memory_id, blob))
                    else:
                        self.current(record, gid, cid)
                elif action in {'approve', 'reject', 'edit', 'delete'}:
                    record = self.repository.record(memory_id=payload.get('memory_id'))
                    if record is None:
                        raise ValueError('memory_not_found')
                    self.current(record, gid, cid)
                    if type(payload.get('version')) is not int or payload['version'] != record.version:
                        return KnowledgeHttpResponse(409, {'ok': False, 'error': 'stale_memory_version_reload'})
                    if action == 'approve':
                        if payload.get('confirmed') is not True:
                            raise ValueError('explicit_confirmation_required')
                        self.repository.approve(memory_id=record.memory_id)
                    elif action == 'reject':
                        self.repository.reject(memory_id=record.memory_id)
                    elif action == 'edit':
                        self.repository.revise(memory_id=record.memory_id, text=self.text(payload.get('text')))
                    else:
                        self.repository.delete(memory_id=record.memory_id)
                        with self.repository._database.transaction() as connection:
                            connection.execute('DELETE FROM memory_evidence WHERE memory_id=?', (record.memory_id,))
                        self.repository.checkpoint()
                else:
                    raise ValueError('unknown_action')
                return KnowledgeHttpResponse(200, self.view(gid, cid, sensitivity))
        except SemanticMemoryUnavailable as exc:
            self.semantic_ready = False
            self.semantic_failure = str(exc)
            return KnowledgeHttpResponse(503, {'ok': False, 'error': str(exc), 'semantic_memory': self.semantic_status()})
        except (PermissionError, GroupNotFoundError, RuntimeError) as exc:
            if isinstance(exc, MemoryRepositoryError):
                return KnowledgeHttpResponse(409, {'ok': False, 'error': 'memory_transition_rejected'})
            return KnowledgeHttpResponse(403, {'ok': False, 'error': 'memory_scope_or_source_unavailable'})
        except (ValueError, TypeError, KeyError):
            return KnowledgeHttpResponse(400, {'ok': False, 'error': 'invalid_memory_request'})

    @staticmethod
    def text(value):
        if not isinstance(value, str) or not value.strip() or len(value) > 4000:
            raise ValueError('invalid_text')
        return value.strip()
