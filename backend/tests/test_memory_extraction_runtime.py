import asyncio
import hashlib
import json

from backend.groups import GroupRepository
from backend.learning import LearningService
from backend.memory_api import MemoryService
from backend.knowe_rag.crypto import MasterKey
from backend.task_runtime.service import TaskRuntimeService


def test_real_runtime_approval_resume_cancel_and_candidate_boundary(tmp_path):
    groups=GroupRepository(tmp_path)
    group=groups.create_group('Synthetic extraction')
    key=MasterKey(b'y'*32)
    learning=LearningService(tmp_path,groups,master_key=key)
    state=learning.mutate(group.group_id,{'revision':0,'action':'course','title':'Math'})
    cid=state['courses'][0]['id']
    runtime=TaskRuntimeService(tmp_path,groups,card_master_key=key)
    calls=[]
    during_call=[]
    class Projection:
        def __init__(self,generate):self.generate=generate
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def extract(self,text):
            result=json.loads(self.generate([{'role':'user','content':text}]))
            return [item['text'] for item in result['memory']]
    def factory(**kwargs):return Projection(kwargs['generate'])
    class Gateway:
        def __init__(self,authority):self.authority=authority
        def preview_operation_scope(self,*,messages):return {'request_digest':hashlib.sha256(json.dumps(messages,sort_keys=True).encode()).hexdigest()}
        async def aclose(self):pass
        async def chat(self,messages,**kwargs):
            assert self.authority is not None and self.authority.group_id==group.group_id
            calls.append(messages)
            if during_call:
                await asyncio.to_thread(during_call.pop(0))
            return {'choices':[{'message':{'content':json.dumps({'memory':[{'text':'Synthetic inferred preference'}]})}}]}
    memory=MemoryService(tmp_path,groups,key,learning=learning,semantic_factory=factory,runtime=runtime)
    memory.extractions.gateway_factory=lambda authority,gid:Gateway(authority)
    async def run():
        query=f'group_id={group.group_id}&course_id={cid}&sensitivity=personal'
        async def act(payload):return await memory.dispatch('POST','/api/memory/action',query,json.dumps(payload).encode())
        proposed=await act({'action':'extract','text':'Synthetic evidence'})
        assert proposed.status==200 and not calls and not proposed.body['candidates']
        pending=proposed.body['pending_operations'][0]
        denied=await act({'action':'extract','operation_id':pending['operation_id']})
        assert denied.status==403 and not calls
        runtime.resolve_approval(group.group_id,pending['approval_id'],approved=True,capabilities=('provider_egress',),request_key='synthetic-approval')
        resumed=await act({'action':'extract','operation_id':pending['operation_id']})
        assert resumed.status==200 and len(calls)==1 and len(resumed.body['candidates'])==1 and not resumed.body['records']
        assert (await act({'action':'extract','operation_id':pending['operation_id']})).status==200
        assert len(calls)==1
        second=await act({'action':'extract','text':'Another synthetic evidence'})
        pending2=second.body['pending_operations'][0]
        cancelled=await act({'action':'cancel_extraction','operation_id':pending2['operation_id']})
        assert cancelled.status==200 and not cancelled.body['pending_operations']
        assert len(calls)==1
        # A real cancellation from another thread wins while the model worker
        # is in flight. Its returned facts must never reach the repository.
        third=await act({'action':'extract','text':'Cancellation race evidence'})
        pending3=third.body['pending_operations'][0]
        runtime.resolve_approval(group.group_id,pending3['approval_id'],approved=True,capabilities=('provider_egress',),request_key='race-approval')
        during_call.append(lambda:memory.extractions.cancel(group.group_id,cid,pending3['operation_id']))
        assert (await act({'action':'extract','operation_id':pending3['operation_id']})).status==403
        assert memory.extractions.read(pending3['operation_id'])['state']=='cancelled'
        assert len(memory.repository.candidates(group_id=group.group_id,course_id=cid))==1
        fourth=await act({'action':'extract','text':'Course deletion race evidence'})
        pending4=fourth.body['pending_operations'][0]
        runtime.resolve_approval(group.group_id,pending4['approval_id'],approved=True,capabilities=('provider_egress',),request_key='course-race-approval')
        during_call.append(lambda:learning.mutate(group.group_id,{'revision':learning.read(group.group_id)['revision'],'action':'delete_course','course_id':cid}))
        assert (await act({'action':'extract','operation_id':pending4['operation_id']})).status==403
        assert len(memory.repository.candidates(group_id=group.group_id,course_id=cid))==1
    try:asyncio.run(run())
    finally:memory.close();learning.close();runtime.close()
