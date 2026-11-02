"""Synthetic local product API tests; no model transport is used."""
import asyncio
import json
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest
from backend.learning import LearningService, SchoolGateway
from backend.memory_api import MemoryService
from backend.groups import GroupLifecycle
from backend.knowe_rag.crypto import MasterKey


class Groups:
    def __init__(self):
        self.rows = {gid: SimpleNamespace(group_id=gid, lifecycle=GroupLifecycle.ACTIVE,
                         course_bindings=[], source_links=[]) for gid in ('g1', 'g2')}
    def get(self, gid):
        if gid not in self.rows:
            raise PermissionError('unknown_group')
        return self.rows[gid]
    def list_groups(self): return list(self.rows.values())
    def delete_transaction(self, gid): return None


@pytest.fixture
def service(tmp_path):
    groups, key = Groups(), MasterKey(b'q' * 32)
    learning = LearningService(tmp_path, groups, master_key=key)
    state = learning.mutate('g1', {'revision': 0, 'action': 'course', 'title': 'Synthetic math'})
    cid = state['courses'][0]['id']
    memory = MemoryService(tmp_path, groups, key, learning=learning)
    yield memory, learning, cid, groups
    memory.close(); learning.close()


def call(memory, cid, action=None, gid='g1', sensitivity='personal'):
    return asyncio.run(memory.dispatch('POST' if action else 'GET', '/api/memory/action' if action else '/api/memory/state',
        f'group_id={gid}&course_id={cid}&sensitivity={sensitivity}', json.dumps(action).encode('utf-8') if action else b''))


def test_candidate_confirm_edit_tombstone_scope_and_stale(service):
    memory, learning, cid, groups = service
    response = call(memory, cid, {'action': 'write', 'text': 'I prefer examples', 'inferred': True})
    assert response.status == 200 and not response.body['records']
    candidate = response.body['candidates'][0]
    assert call(memory, cid, gid='g2').status == 403
    response = call(memory, cid, {'action': 'approve', 'memory_id': candidate['id'], 'version': candidate['version'], 'confirmed': True})
    approved = response.body['records'][0]
    assert approved['version'] > candidate['version']
    assert call(memory, cid, {'action': 'edit', 'memory_id': approved['id'], 'version': candidate['version'], 'text': 'stale'}).status == 409
    response = call(memory, cid, {'action': 'edit', 'memory_id': approved['id'], 'version': approved['version'], 'text': 'I prefer worked examples'})
    edited = response.body['candidates'][0]
    assert call(memory, cid, {'action': 'delete', 'memory_id': edited['id'], 'version': edited['version']}).status == 200
    assert call(memory, cid, {'action': 'write', 'text': edited['text'], 'inferred': True}).status == 409
    groups.rows['g1'].lifecycle = GroupLifecycle.ARCHIVED
    assert call(memory, cid, {'action': 'write', 'text': 'new', 'inferred': True}).status == 403


def test_current_source_version_required_and_not_rebound(service):
    memory, learning, cid, groups = service
    groups.rows['g1'].course_bindings.append(SimpleNamespace(course_id=cid))
    class Knowledge:
        version = 1
        store = SimpleNamespace(_database=SimpleNamespace(_lock=__import__("threading").RLock()))
        def require_source(self, gid, course_id, source_id):
            if source_id != 'source-a': raise PermissionError()
            return {'version': self.version}
    memory.knowledge = Knowledge()
    payload = {'action': 'write', 'text': 'Source derived preference', 'inferred': True,
               'source': {'kind': 'knowledge', 'source_id': 'source-a', 'version': 1}}
    candidate = call(memory, cid, payload).body['candidates'][0]
    memory.knowledge.version = 2
    assert not call(memory, cid).body['candidates']
    assert call(memory, cid, {'action': 'approve', 'memory_id': candidate['id'], 'version': 1, 'confirmed': True}).status == 403
    payload['source']['version'] = 2
    assert call(memory, cid, payload).status == 403  # dedup may not rebind older evidence


def test_delete_course_reconciles_and_wrong_learning_key_rejected(service, tmp_path):
    memory, learning, cid, _ = service
    call(memory, cid, {'action': 'write', 'text': 'Synthetic private preference', 'inferred': True})
    learning.mutate('g1', {'revision': learning.read('g1')['revision'], 'action': 'delete_course', 'course_id': cid})
    memory.reconcile()
    assert not memory.repository.candidates(group_id='g1', course_id=cid)
    with pytest.raises(Exception):
        LearningService(tmp_path, learning.groups, master_key=MasterKey(b'z' * 32))
    assert b'Synthetic private preference' not in (tmp_path / 'memory' / 'memory.sqlite').read_bytes()


def test_edit_sensitive_public_memory_requires_confirmation(service):
    memory, _, cid, _ = service
    response = call(memory, cid, {'action': 'write', 'text': 'Matrices are rectangular', 'inferred': False, 'sensitivity': 'public'}, sensitivity='public')
    row = response.body['records'][0]
    response = call(memory, cid, {'action': 'edit', 'memory_id': row['id'], 'version': row['version'], 'text': '成绩 92'}, sensitivity='public')
    assert response.status == 200 and not response.body['records']
    assert len(response.body['candidates']) == 1


def test_school_mock_opt_in_ranking_and_unknown_grade():
    school = SchoolGateway().snapshot()
    assert school['mode'] == 'mock' and school['read_only']
    assert school['timetable'] and school['authentication']['authenticated'] is False
    assert school['grades'][0]['grade'] is None and school['ranking'] is None
    assert SchoolGateway().snapshot(include_ranking=True)['ranking']['status'] == 'unknown'


def test_semantic_unavailability_is_explicit_and_does_not_write(service):
    memory, _, cid, _ = service
    response = call(memory, cid, {'action':'extract','text':'Synthetic inferred preference'})
    assert response.status == 503 and response.body['semantic_memory']['available'] is False
    assert not call(memory, cid).body['candidates']


def test_confirmation_holds_course_authority_through_repository_transition(service, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    memory, learning, cid, _ = service
    candidate=call(memory,cid,{'action':'write','text':'Threaded preference','inferred':True}).body['candidates'][0]
    entered, release, deleting, deleted = (threading.Event() for _ in range(4))
    original=memory.repository.approve
    def paused_approve(**kwargs):
        entered.set()
        assert release.wait(3)
        return original(**kwargs)
    monkeypatch.setattr(memory.repository,'approve',paused_approve)
    def delete_course():
        deleting.set()
        learning.mutate('g1',{'revision':learning.read('g1')['revision'],'action':'delete_course','course_id':cid})
        deleted.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        approval=pool.submit(call,memory,cid,{'action':'approve','memory_id':candidate['id'],'version':candidate['version'],'confirmed':True})
        assert entered.wait(3)
        deletion=pool.submit(delete_course)
        assert deleting.wait(3)
        try:
            assert not deleted.wait(.1), 'course authority released before repository transition'
        finally:
            release.set()
        assert approval.result(3).status==200
        deletion.result(3)
    memory.reconcile()
    assert memory.repository.record(memory_id=candidate['id']) is None


def test_semantic_configuration_success_failure_and_retry_are_distinct(service):
    from backend.knowe_memory.mem0_bridge import SemanticMemoryUnavailable
    memory, _, cid, _ = service
    calls=[]
    class Projection:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def rebuild(self, records): pass
        def search(self,query,**kwargs): return []
    def configured(**kwargs):
        calls.append(kwargs)
        return Projection()
    memory.semantic_factory=configured
    status=call(memory,cid).body['semantic_memory']
    assert status['configured'] and status['status']=='configured_unverified'
    assert not status['available'] and status['reason'] is None and not calls
    status=call(memory,cid,{'action':'search','query':'synthetic'}).body['semantic_memory']
    assert status['status']=='ready' and status['available'] and len(calls)==1
    def unavailable(**kwargs):
        raise SemanticMemoryUnavailable('local_embedding_pack_or_runtime_unavailable')
    memory.semantic_factory=unavailable
    response=call(memory,cid,{'action':'search','query':'synthetic'})
    assert response.status==503
    status=call(memory,cid).body['semantic_memory']
    assert status['status']=='unavailable' and not status['available']
    assert status['reason']=='local_embedding_pack_or_runtime_unavailable'
    memory.semantic_factory=configured
    status=call(memory,cid,{'action':'search','query':'synthetic'}).body['semantic_memory']
    assert status['status']=='ready' and status['reason'] is None
