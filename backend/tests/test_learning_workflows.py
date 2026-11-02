from datetime import datetime, timezone
from types import SimpleNamespace
import asyncio
import json
import pytest
from backend.learning import LearningService
from backend.groups import GroupLifecycle
from backend.knowe_rag.crypto import MasterKey

class Groups:
    def __init__(self):
        self.rows = {x: SimpleNamespace(group_id=x, lifecycle=GroupLifecycle.ACTIVE) for x in ('g1','g2')}
    def get(self, gid):
        if gid not in self.rows: raise PermissionError()
        return self.rows[gid]
    def list_groups(self): return list(self.rows.values())
    def delete_transaction(self, gid): return None

@pytest.fixture
def service(tmp_path):
    value = LearningService(tmp_path, Groups(), master_key=MasterKey(b'a'*32))
    yield value
    value.close()

NOW = datetime(2030,1,1,tzinfo=timezone.utc)
def action(s, **payload):
    return s.mutate('g1', {'revision':s.read('g1')['revision'], **payload}, now=NOW)

def test_schedule_conflicts_replan_and_ics(service):
    s=action(service,action='course',title='Private course')
    cid=s['courses'][0]['id']
    action(service,action='availability',slots=[{'start':'2030-01-01T08:00:00Z','end':'2030-01-01T09:00:00Z'}])
    s=action(service,action='assessment',course_id=cid,title='Exam',minutes=90,deadline='2030-01-02T00:00:00Z')
    assert s['conflicts'][0]['unallocated_minutes']==30
    assert len(s['tasks'][0]['sessions'])==1
    assert 'DTSTART:20300101T080000Z' in service.calendar('g1',s)
    s=action(service,action='skip',task_id=s['tasks'][0]['id'])
    assert not s['conflicts']
    assert not s['tasks'][0]['sessions']
    assert service.read('g2')['courses']==[]
    assert b'Private course' not in (service.db.execute('SELECT sealed FROM learning_state').fetchone()[0])

def test_practice_review_and_answer_boundary(service):
    s=action(service,action='course',title='Math')
    cid=s['courses'][0]['id']
    s=action(service,action='question',course_id=cid,topic='Addition',prompt='1+1?',answer='2',explanation='two',difficulty=1)
    qid=s['questions'][0]['id']
    s=action(service,action='practice',question_id=qid,answer='2')
    assert s['attempts'][0]['correct']
    assert len(s['tasks'])==1
    assert s['tasks'][0]['not_before']=='2030-01-02T00:00:00+00:00'
    response=asyncio.run(service.dispatch('GET','/api/learning/state','group_id=g1',b''))
    assert 'answer' not in response.body['state']['questions'][0]
    s=action(service,action='practice',question_id=qid,answer='wrong')
    assert len([x for x in s['tasks'] if x['status']=='pending'])==1

def test_reject_stale_overlap_archived_and_evolution(service):
    with pytest.raises(RuntimeError): service.mutate('g1',{'revision':99,'action':'course','title':'X'})
    with pytest.raises(ValueError): action(service,action='availability',slots=[{'start':'2030-01-01T08:00Z','end':'2030-01-01T10:00Z'},{'start':'2030-01-01T09:00Z','end':'2030-01-01T11:00Z'}])
    s=action(service,action='candidate',kind='prompt',text='Explain sources')
    with pytest.raises(ValueError): action(service,action='approve_candidate',candidate_id=s['candidates'][0]['id'],confirmed=True)
    service.groups.rows['g1'].lifecycle=GroupLifecycle.ARCHIVED
    with pytest.raises(PermissionError): action(service,action='course',title='X')
    del service.groups.rows['g1']
    service.reconcile()
    assert service.db.execute('SELECT count(*) FROM learning_state').fetchone()[0]==0
