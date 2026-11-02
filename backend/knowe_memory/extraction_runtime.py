"""Version-bound Mem0 extraction with real runtime approval and governed gateway."""
import asyncio
import json
import uuid

from backend.commercial_gateway import GatewayDenied, LegacyToolAuthority
from backend.task_runtime.contracts import EvidenceRef, RiskLevel, RunKind, TaskStatus
from backend.rag_runtime import configured_gateway
from backend.knowe_rag.crypto import field_aad
from .mem0_bridge import SemanticMemoryUnavailable


class MemoryExtractionRuntime:
    def __init__(self, service, runtime, gateway_factory=configured_gateway):
        self.service,self.runtime,self.gateway_factory=service,runtime,gateway_factory
        self.lock=asyncio.Lock()
        service.repository._database.initialize('CREATE TABLE IF NOT EXISTS memory_operations (operation_id TEXT PRIMARY KEY, sealed BLOB NOT NULL);')
    def save(self, oid, data):
        blob=self.service.key.seal(json.dumps(data,ensure_ascii=False).encode('utf-8'),aad=field_aad(oid,'memory.operation'))
        with self.service.repository._database.transaction() as connection:
            connection.execute('INSERT OR REPLACE INTO memory_operations VALUES (?,?)',(oid,blob))
    def read(self, oid):
        with self.service.repository._database.transaction(immediate=False) as connection:
            row=connection.execute('SELECT sealed FROM memory_operations WHERE operation_id=?',(oid,)).fetchone()
        return json.loads(self.service.key.open(row['sealed'],aad=field_aad(oid,'memory.operation'))) if row else None
    def pending(self,gid,cid):
        with self.service.repository._database.transaction(immediate=False) as connection:
            ids=[r['operation_id'] for r in connection.execute('SELECT operation_id FROM memory_operations')]
        result=[]
        for oid in ids:
            op=self.read(oid)
            if op['gid']!=gid or op['cid']!=cid or op['state']!='pending':continue
            approval=self.runtime.store.get_approval(op['approval_id'])
            if approval and approval.status in {'pending','approved'}:
                result.append({'operation_id':oid,'approval_id':approval.approval_id,'run_id':op['run_id'],'status':approval.status})
        return result
    def reconcile(self):
        with self.service.repository._database.transaction(immediate=False) as connection:
            ids=[r['operation_id'] for r in connection.execute('SELECT operation_id FROM memory_operations')]
        for oid in ids:
            op=self.read(oid)
            try:
                self.service.scope(op['gid'],op['cid'])
                self.service.source(op['gid'],op['cid'],op['source'])
            except (RuntimeError,ValueError,PermissionError):
                with self.service.repository._database.transaction() as connection:
                    connection.execute('DELETE FROM memory_operations WHERE operation_id=?',(oid,))
            else:
                if op['state']=='executing':
                    op['state']='interrupted';op.pop('text',None);op.pop('messages',None);self.save(oid,op)
    def cancel(self,gid,cid,oid):
        with self.service.authority_guard():
            self.service.scope(gid,cid,True)
            op=self.read(oid)
            if not op or op['gid']!=gid or op['cid']!=cid:
                raise PermissionError('memory_operation_outside_scope')
            if op['state']=='cancelled':return
            self.runtime.cancel(gid,op['run_id'],request_key=oid+':cancel')
            op['state']='cancelled';op.pop('text',None);op.pop('messages',None);self.save(oid,op)
    async def extract(self,gid,cid,sensitivity,payload):
        if self.runtime is None or self.service.semantic_factory is None:
            raise SemanticMemoryUnavailable('memory_extraction_runtime_unavailable')
        async with self.lock:
            oid=payload.get('operation_id')
            if not oid:
                text=self.service.text(payload.get('text'))
                source=payload.get('source',{'kind':'manual','version':1})
                self.service.scope(gid,cid,True);self.service.source(gid,cid,source)
                revision=self.service.learning.read(gid)['revision']
                captured=[]
                def capture(messages):
                    captured.append(messages)
                    return '{"memory": []}'
                def preview():
                    with self.service.semantic_factory(gid=gid,cid=cid,sensitivity=sensitivity.value,operation='extract',generate=capture) as p:
                        p.extract(text)
                await asyncio.to_thread(preview)
                if len(captured)!=1:raise SemanticMemoryUnavailable('mem0_exact_prompt_unavailable')
                gateway=self.gateway_factory(None,gid)
                try:scope=gateway.preview_operation_scope(messages=captured[0])
                finally:await gateway.aclose()
                with self.service.authority_guard():
                    self.service.scope(gid,cid,True);self.service.source(gid,cid,source)
                    if self.service.learning.read(gid)['revision']!=revision:
                        raise GatewayDenied('memory_course_revision_changed')
                    oid='memory_'+uuid.uuid4().hex
                    run=self.runtime.submit(gid,title='Memory candidate extraction',request_key=oid,
                        kind=RunKind.PRIMARY,read_only=True,risk=RiskLevel.HIGH,public_summary='Awaiting scoped memory extraction approval')
                    self.runtime.schedule();run=self.runtime.store.get_run(run.run_id)
                    if run.status is not TaskStatus.RUNNING:raise GatewayDenied('memory_runtime_capacity_unavailable')
                    approval=self.runtime.request_approval(gid,run.run_id,kind='tool',risk=RiskLevel.HIGH,
                        summary='Approve one exact memory extraction model call; resulting facts require separate confirmation',
                        request_key='approval:'+oid,requested_capabilities=('provider_egress',),tool_name='provider_egress',operation_scope=scope,purpose='provider_egress')
                    self.save(oid,{'gid':gid,'cid':cid,'sensitivity':sensitivity.value,'text':text,'source':source,
                        'messages':captured[0],'scope':scope,'revision':revision,
                        'run_id':run.run_id,'task_id':run.task_id,'approval_id':approval.approval_id,'state':'pending'})
                return
            with self.service.authority_guard():
                op=self.read(oid)
                if not op or op['gid']!=gid or op['cid']!=cid or op['sensitivity']!=sensitivity.value:
                    raise PermissionError('memory_operation_outside_scope')
                if op['state']=='completed':return
                if op['state']!='pending':raise GatewayDenied('memory_operation_interrupted_new_approval_required')
                self.service.scope(gid,cid,True);self.service.source(gid,cid,op['source'])
                if self.service.learning.read(gid)['revision']!=op['revision']:raise GatewayDenied('memory_course_revision_changed')
                approval=self.runtime.store.get_approval(op['approval_id'])
                if not approval or approval.status!='approved':raise GatewayDenied('memory_extraction_not_approved')
                grant=self.runtime.store.grant_for_approval(approval.approval_id)
                if not grant or grant.operation_scope!=op['scope']:raise GatewayDenied('memory_exact_grant_unavailable')
                self.runtime.schedule();run=self.runtime.store.get_run(op['run_id'])
                if not run or run.status is not TaskStatus.RUNNING or run.group_id!=gid or run.task_id!=op['task_id']:
                    raise GatewayDenied('memory_extraction_run_unavailable')
                authority=LegacyToolAuthority(group_id=gid,task_id=run.task_id,run_id=run.run_id,runtime=self.runtime,approval_id=approval.approval_id,grant_id=grant.grant_id)
                gateway=self.gateway_factory(authority,gid)
                loop=asyncio.get_running_loop()
                def generate(messages):
                    if gateway.preview_operation_scope(messages=messages)!=op['scope']:
                        raise GatewayDenied('memory_prompt_changed')
                    result=asyncio.run_coroutine_threadsafe(gateway.chat(messages,max_tokens=2048),loop).result(timeout=180)
                    return result['choices'][0]['message']['content']
                def execute():
                    with self.service.semantic_factory(gid=gid,cid=cid,sensitivity=sensitivity.value,operation='extract',generate=generate) as p:
                        return p.extract(op['text'])
                op['state']='executing';self.save(oid,op)
            try:
                facts=await asyncio.to_thread(execute)
                with self.service.authority_guard():
                    self.service.scope(gid,cid,True);self.service.source(gid,cid,op['source'])
                    current=self.runtime.store.get_run(run.run_id)
                    if self.service.learning.read(gid)['revision']!=op['revision'] or current.status is not TaskStatus.RUNNING:
                        raise GatewayDenied('memory_commit_authority_changed')
                    self.service.commit_facts(gid,cid,sensitivity,op['source'],facts)
                    self.runtime.transition(gid,run.run_id,TaskStatus.VERIFYING,idempotency_key=oid+':verify',summary='Memory candidate schema and current source verified')
                    self.runtime.finalize_trusted_execution(gid,run.run_id,execution_key=oid,outcome=TaskStatus.PARTIAL,
                        verifier_approved=True,evidence=(EvidenceRef(evidence_id=oid,source_id=op['source'].get('source_id','manual'),
                            version_id=str(op['source']['version']),locator='course:'+cid),),summary='Memory candidates stored; factual confirmation pending')
                    op['state']='completed';op.pop('text',None);op.pop('messages',None);self.save(oid,op)
            except BaseException:
                with self.service.authority_guard():
                    latest=self.read(oid)
                    if latest is not None and latest['state']!='cancelled':
                        op['state']='interrupted';op.pop('text',None);op.pop('messages',None);self.save(oid,op)
                self.runtime.finalize_trusted_execution(gid,run.run_id,execution_key=oid,outcome=TaskStatus.FAILED,
                    verifier_approved=False,summary='Memory extraction failed or cancelled; no automatic retry')
                raise
            finally:await gateway.aclose()
