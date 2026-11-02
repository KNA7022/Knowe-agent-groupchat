import { useEffect, useRef, useState } from 'react';
import { runtimeFetch } from '../shared/runtimeFetch';
import { runtimeHttpBase } from '../shared/runtimeEndpoints';
import './ProtectedTaskPanel.css';

type Card = { approval_id: string; generation: number; digest: string; expires_at: string; status: string;
  proposal: {instruction: string; target_role: string} };
type Snapshot = { cards: Card[]; runs: {run_id: string; status: string; blocking_reason: string}[];
  messages: {event_id: string; content: string; status: string}[] };
const labels: Record<string, string> = {queued: '排队中', routing: '准备中', running: '执行中',
  waiting_approval: '等待审批', verifying: '核验中', succeeded: '已完成', partial: '部分完成',
  failed: '执行失败', cancelled: '已取消', paused: '已暂停（重启后的任务不会自动重放，请创建新提案）'};

export function ProtectedTaskPanel({groupId}: {groupId: string}) {
  const [snapshot, setSnapshot] = useState<Snapshot>({cards: [], runs: [], messages: []});
  const [instruction, setInstruction] = useState('');
  const [role, setRole] = useState('knowledge');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const epoch = useRef(0);
  const proposalKey = useRef(crypto.randomUUID());
  async function request(action: string, payload: Record<string, unknown> = {}) {
    const response = await runtimeFetch(`${runtimeHttpBase()}/task-runtime/${action}`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({...payload, group_id: groupId}),
    });
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || data.message || '受保护任务服务不可用');
    return data as Snapshot;
  }
  useEffect(() => {
    const token = ++epoch.current;
    setSnapshot({cards: [], runs: [], messages: []}); setInstruction(''); setError('');
    proposalKey.current = crypto.randomUUID();
    const refresh = () => request('protected-snapshot').then(data => {
      if (epoch.current === token) setSnapshot(data);
    }).catch(e => {if (epoch.current === token) setError(String(e.message));});
    void refresh();
    const timer = window.setInterval(() => {void refresh();}, 2000);
    return () => {++epoch.current; window.clearInterval(timer);};
  }, [groupId]);
  async function act(action: string, payload: Record<string, unknown>) {
    if (busy) return;
    const token = epoch.current;
    setBusy(true); setError('');
    try {
      await request(action, payload);
      const data = await request('protected-snapshot');
      if (token === epoch.current) setSnapshot(data);
      if (action === 'protected-propose' && token === epoch.current) {
        setInstruction(''); proposalKey.current = crypto.randomUUID();
      }
    } catch (e) {if (token === epoch.current) setError(e instanceof Error ? e.message : String(e));}
    finally {if (token === epoch.current) setBusy(false);}
  }
  return <section className="protected-task-panel" aria-label="受保护群组任务">
    <h3>受保护任务</h3>
    <p>指令在本地加密保存。审批绑定显示的版本和群组资料版本；知识任务可只读检索当前群组资料并核验引用，执行需要已配置的模型。</p>
    {error && <p role="alert">{error}</p>}
    <label>任务角色 <select value={role} onChange={e => {setRole(e.target.value); proposalKey.current = crypto.randomUUID();}}>
      <option value="knowledge">知识助手</option><option value="planning">规划助手（需群组成员）</option>
      <option value="coach">学习教练（需群组成员）</option>
    </select></label>
    <label>任务指令<textarea value={instruction} maxLength={16384} placeholder="写下需要完成的具体任务"
      onChange={e => {setInstruction(e.target.value); proposalKey.current = crypto.randomUUID();}} /></label>
    <button disabled={busy || !instruction.trim()} onClick={() => void act('protected-propose', {
      request_key: proposalKey.current, proposal: {target_role: role, instruction},
    })}>生成审批提案</button>
    {snapshot.cards.map(card => <article key={card.approval_id}>
      <strong>{card.status === 'approved' ? '已审批' : '待审批'} · 版本 {card.generation} · {card.proposal.target_role}</strong>
      <pre>{card.proposal.instruction}</pre><p>有效至 {new Date(card.expires_at).toLocaleString()}</p>
      {(card.status === 'approved' ? [true] : [true, false]).map(approved => <button key={String(approved)} disabled={busy} onClick={() => void act('protected-resolve', {
        approval_id: card.approval_id, generation: card.generation, digest: card.digest,
        approved, request_key: `ui:${card.approval_id}:${card.generation}:${approved}`,
      })}>{approved ? card.status === 'approved' ? '继续启动已批版本' : '批准并执行此版本' : '拒绝'}</button>)}
    </article>)}
    {snapshot.runs.map(run => <p key={run.run_id}>{labels[run.status] || run.status} {run.blocking_reason}
      {!['succeeded', 'partial', 'failed', 'cancelled'].includes(run.status) && <button disabled={busy}
        onClick={() => void act('control', {run_id: run.run_id, command: 'cancel', request_key: `cancel:${run.run_id}`})}>取消任务</button>}
    </p>)}
    {snapshot.messages.map(message => <article key={message.event_id}><strong>任务结果 · {labels[message.status.toLowerCase()] || message.status}</strong><pre>{message.content}</pre></article>)}
  </section>;
}
