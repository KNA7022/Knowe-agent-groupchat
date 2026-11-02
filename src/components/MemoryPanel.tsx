import { useEffect, useRef, useState } from 'react';
import { runtimeFetch } from '../shared/runtimeFetch';
import { runtimeHttpBase } from '../shared/runtimeEndpoints';

type Memory = { id: string; text: string; version: number; status: string; sensitivity: string;
  expires_at: string | null; source: {kind: string; version: number; source_id?: string} };
type MemoryOperation = {operation_id:string;approval_id:string;run_id:string;status:string};
type MemoryState = { records: Memory[]; candidates: Memory[]; pending_operations?:MemoryOperation[]; semantic_memory?: {available:boolean;status?:'configured_unverified'|'ready'|'unavailable';reason?:string;required_version?:string} };
export function MemoryPanel({ groupId, courseId }: { groupId: string; courseId: string }) {
  const [state, setState] = useState<MemoryState | null>(null);
  const [sensitivity, setSensitivity] = useState('personal');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const epoch = useRef(0);
  async function request(action?: Record<string, unknown>) {
    const requestEpoch = epoch.current;
    const response = await runtimeFetch(`${runtimeHttpBase()}/api/memory/${action ? 'action' : 'state'}?group_id=${encodeURIComponent(groupId)}&course_id=${encodeURIComponent(courseId)}&sensitivity=${sensitivity}`,
      action ? {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(action)} : undefined);
    const data = await response.json();
    if (!response.ok || !data.ok) {
      if (data.semantic_memory && requestEpoch === epoch.current) setState(previous => previous ? {...previous, semantic_memory:data.semantic_memory} : previous);
      throw new Error(data.error || '记忆服务暂不可用');
    }
    if (!Array.isArray(data.records) || !Array.isArray(data.candidates)) throw new Error('记忆响应格式错误');
    return data as MemoryState;
  }
  useEffect(() => {
    const token = ++epoch.current;
    setState(null); setError(''); setBusy(false);
    if (groupId && courseId) request().then(data => {if (epoch.current === token) setState(data);})
      .catch(e => {if (epoch.current === token) setError(e.message);});
    return () => {epoch.current++;};
  }, [groupId, courseId, sensitivity]);
  async function act(action: Record<string, unknown>) {
    if (busy) return;
    const token = epoch.current;
    setBusy(true); setError('');
    try {const data = await request(action); if (epoch.current === token) setState(data);}
    catch (e) {if (epoch.current === token) setError(e instanceof Error ? e.message : String(e));}
    finally {if (epoch.current === token) setBusy(false);}
  }
  async function approveOperation(operation: MemoryOperation, approved: boolean) {
    const token=epoch.current;
    if(busy)return;
    setBusy(true);setError('');
    try {
      if(approved && operation.status!=='approved') {
        const response=await runtimeFetch(`${runtimeHttpBase()}/task-runtime/approval`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({group_id:groupId,run_id:operation.run_id,approval_id:operation.approval_id,approved,capabilities:approved?['provider_egress']:[],request_key:`memory-${approved?'approve':'reject'}-${crypto.randomUUID()}`})});
        if(!response.ok)throw new Error('审批失败，请重新加载当前版本');
      }
      if(token!==epoch.current)return;
      const data=await request(approved?{action:'extract',operation_id:operation.operation_id}:{action:'cancel_extraction',operation_id:operation.operation_id});
      if(token===epoch.current)setState(data);
    }catch(e){if(token===epoch.current)setError(e instanceof Error?e.message:String(e));}
    finally{if(token===epoch.current)setBusy(false);}
  }
  return <article aria-label="课程记忆"><h3>课程记忆与候选确认</h3>
    <p>默认使用本地精确召回。推断、偏好与成绩先进入候选，确认后才会参与本课程召回；Mem0 使用本地嵌入；批准后将指定内容发送至配置的远程模型抽取。</p>
    {state?.semantic_memory && <p role="status">Mem0 语义能力：{state.semantic_memory.status==='configured_unverified'?'已配置，尚未验证':state.semantic_memory.available?'已验证可用':`验证不可用（${state.semantic_memory.reason || '未配置'}）`} · SDK {state.semantic_memory.required_version}</p>}
    {!courseId ? <p>请先添加或选择课程。</p> : <>
      <label>召回范围<select value={sensitivity} onChange={e=>setSensitivity(e.target.value)}><option value="public">公开事实</option><option value="personal">个人偏好</option><option value="sensitive">敏感信息</option></select></label>
      {error && <p role="alert">{error}<button onClick={()=>{const token=epoch.current;void request().then(d=>{if(token===epoch.current){setState(d);setError('');}}).catch(e=>{if(token===epoch.current)setError(e.message);});}}>重新加载</button></p>}
      <fieldset disabled={busy}><form onSubmit={e=>{e.preventDefault();const f=new FormData(e.currentTarget);void act({action:'write',text:f.get('text'),sensitivity:f.get('sensitivity'),inferred:f.get('inferred')==='on',ttl_seconds:Number(f.get('days'))*86400,source:{kind:'manual',version:1}});}}>
        <textarea name="text" aria-label="记忆内容" placeholder="记录事实或待确认偏好" required maxLength={4000}/>
        <select name="sensitivity" aria-label="记忆敏感度" defaultValue="personal"><option value="public">公开事实</option><option value="personal">个人偏好</option><option value="sensitive">敏感信息</option></select>
        <label><input name="inferred" type="checkbox" defaultChecked/>这是推断，需确认</label><label>有效天数<input name="days" type="number" min="1" max="365" defaultValue="90" required/></label><button>保存记忆</button><button type="button" onClick={e=>{if(!e.currentTarget.form)return;const f=new FormData(e.currentTarget.form);void act({action:'extract',text:f.get('text'),source:{kind:'manual',version:1}});}}>通过 Mem0 提取待确认候选</button>
      </form>
      {state?.pending_operations?.map(operation=><div key={operation.operation_id} role="status"><p>提取待审批：将发送已绑定的证据与精确提示给当前配置模型（最多 2048 输出 token）。生成的候选仍需逐条确认。</p><button onClick={()=>approveOperation(operation,true)}>{operation.status==='approved'?'继续已批准提取':'批准此次模型调用并提取'}</button><button onClick={()=>approveOperation(operation,false)}>拒绝此次调用</button></div>)}
      <form onSubmit={e=>{e.preventDefault();void act({action:'search',query:new FormData(e.currentTarget).get('query')});}}><input name="query" aria-label="记忆语义查询" placeholder="查询已批准记忆" required/><button>Mem0 语义召回</button></form>
      {state && [...state.candidates, ...state.records].map(m=><div key={m.id} className="learning-task"><p>{m.text} · {m.status} · 版本 {m.version}</p><small>来源：{m.source.kind==='manual'?'用户录入':m.source.source_id}（版本 {m.source.version}） · 到期 {m.expires_at ? new Date(m.expires_at).toLocaleString() : '无'}</small>
        <form onSubmit={e=>{e.preventDefault();void act({action:'edit',memory_id:m.id,version:m.version,text:new FormData(e.currentTarget).get('text')});}}><input name="text" aria-label="编辑记忆内容" defaultValue={m.text} maxLength={4000} required/><button>修改并检查</button></form>
        {m.status==='candidate' && <><button onClick={()=>act({action:'approve',memory_id:m.id,version:m.version,confirmed:true})}>确认此版本</button><button onClick={()=>act({action:'reject',memory_id:m.id,version:m.version})}>否决</button></>}
        <button onClick={()=>act({action:'delete',memory_id:m.id,version:m.version})}>删除并阻止重新学习</button>
      </div>)}
      {state && !state.records.length && !state.candidates.length && <p>该课程和敏感度范围暂无记忆。</p>}
      </fieldset></>}
  </article>;
}
