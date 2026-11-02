import React, { useEffect, useRef, useState } from 'react';
import { useGroupStore } from '../store/groups';
import { runtimeFetch } from '../shared/runtimeFetch';
import { runtimeHttpBase } from '../shared/runtimeEndpoints';
import './LearningPanel.css';
import type { Group } from '../contract/group';
import { MemoryPanel } from './MemoryPanel';

type Course = { id: string; title: string };
type Task = Course & { status: string; course_id: string; minutes: number; deadline: string; sessions: {start: string; end: string}[] };
type Question = { id: string; course_id: string; topic: string; prompt: string; difficulty: number };
type State = { revision: number; notifications_enabled?: boolean; courses: Course[]; tasks: Task[]; questions: Question[];
  conflicts: { task_id: string; unallocated_minutes: number }[];
  attempts: {id: string; question_id: string; correct: boolean; explanation: string}[];
  mastery: Record<string, {topic: string; score: number; review_at: string; course_id: string}>;
  availability: {start: string; end: string}[];
  candidates: {id: string; kind: string; text: string; status: string; evaluation: unknown}[] };
const localDate = (value: string) => new Date(value).toLocaleString();

export function LearningPanel() {
  const groups = useGroupStore(s => s.order.map(id => s.groups[id]).filter((g): g is Group => g !== undefined && g.lifecycle === 'active'));
  const [gid, setGid] = useState('');
  const [state, setState] = useState<State | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [cid, setCid] = useState('');
  const [answer, setAnswer] = useState('');
  const [notifications, setNotifications] = useState(false);
  const generation = useRef(0);
  const notified = useRef(new Set<string>());
  const selected = groups.some(g => g.group_id === gid) ? gid : groups[0]?.group_id || '';
  async function request(route: string, action?: Record<string, unknown>, extraQuery = '') {
    const response = await runtimeFetch(`${runtimeHttpBase()}/api/learning/${route}?group_id=${encodeURIComponent(selected)}${extraQuery}`,
      action ? {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(action)} : undefined);
    const data = await response.json();
    if (!response.ok || !data.ok) throw new Error(data.error || '学习服务暂不可用');
    if (!data.state || !Number.isInteger(data.state.revision) || !['courses','tasks','questions','conflicts','attempts','availability','candidates'].every(k=>Array.isArray(data.state[k])) || typeof data.state.mastery !== 'object') throw new Error('学习响应格式错误');
    return data;
  }
  useEffect(() => {
    const token = ++generation.current;
    setState(null); setCid(''); setError(''); setBusy(false); setAnswer(''); setNotifications(false);
    if (selected) request('state').then(data => {if (generation.current === token) setState(data.state);})
      .catch(e => {if (generation.current === token) setError(String(e.message));});
    return () => {generation.current++;};
  }, [selected]);
  async function act(action: Record<string, unknown>) {
    if (!state || busy) return;
    const token = generation.current;
    setBusy(true); setError('');
    try { const data = await request('action', {...action, revision: state.revision}); if (token === generation.current) setState(data.state); }
    catch (e) { if (token === generation.current) setError(e instanceof Error ? e.message : String(e)); }
    finally { if (token === generation.current) setBusy(false); }
  }
  useEffect(() => {
    setNotifications(Boolean(state?.notifications_enabled));
  }, [state?.notifications_enabled]);
  useEffect(() => {
    if (window.knowe?.learningNotificationSupported || !notifications || !state || !('Notification' in window) || Notification.permission !== 'granted') return;
    const check = () => {
      const now = Date.now();
      for (const t of state.tasks) for (const session of t.sessions) {
        const key = `${selected}:${t.id}:${session.start}`;
        const delta = new Date(session.start).getTime() - now;
        if (t.status === 'pending' && delta <= 5 * 60000 && delta > -60000 && !notified.current.has(key)) {
          notified.current.add(key);
          try {new Notification('StudyCrew 学习提醒', {body: '有一项学习任务即将开始，请打开学习计划查看。'});} catch {setError('系统通知不可用，请查看应用内计划');}
        }
      }
    };
    check(); const timer = window.setInterval(check, 30000); return () => clearInterval(timer);
  }, [notifications, state, selected]);
  const courseId = state?.courses.some(c => c.id === cid) ? cid : state?.courses[0]?.id || '';
  const mastery = state ? Object.values(state.mastery).filter(m => m.course_id === courseId) : [];
  const question = state?.questions.filter(q => q.course_id === courseId).sort((a,b) => {
    const score = (q: Question) => Math.abs(q.difficulty - (1 + (mastery.find(m => m.topic === q.topic)?.score || 0) * 4));
    return score(a) - score(b);
  }).find(q => !state.attempts.slice(-1).some(a => a.question_id === q.id))
    || state?.questions.find(q => q.course_id === courseId);
  const submit = (e: React.FormEvent<HTMLFormElement>, build: (f: FormData) => Record<string, unknown>) => {
    e.preventDefault(); try { void act(build(new FormData(e.currentTarget))); } catch {setError('请填写有效的日期和时间');}
  };
  return <section className="learning-panel" aria-label="学习计划">
    <header><div><span className="study-eyebrow">Learning workspace</span><h2>课程、计划与复习</h2></div>
      <select aria-label="学习群组" value={selected} onChange={e => setGid(e.target.value)}>{groups.map(g => <option key={g.group_id} value={g.group_id}>{g.name}</option>)}</select></header>
    {!selected && <p>先创建一个学习群组。</p>}
    {error && <p role="alert">{error} <button onClick={() => {const token=generation.current;void request('state').then(d => {if(token===generation.current){setState(d.state);setError('');}}).catch(e => {if(token===generation.current)setError(e.message);});}}>重新加载</button></p>}
    {state && <fieldset disabled={busy}><div className="learning-grid">
      <article><h3>课程与考试</h3><form onSubmit={e => submit(e, f => ({action:'course',title:f.get('title')}))}><input name="title" aria-label="课程名称" placeholder="课程名称" required maxLength={100}/><button>添加课程</button></form>
        <select aria-label="当前课程" value={courseId} onChange={e => setCid(e.target.value)}>{state.courses.map(c => <option value={c.id} key={c.id}>{c.title}</option>)}</select>
        {courseId && <button onClick={()=>act({action:'delete_course',course_id:courseId})}>删除当前课程及学习记录</button>}
        {courseId && <form onSubmit={e => submit(e, f => ({action: f.get('kind'), course_id:courseId,title:f.get('title'),deadline:new Date(String(f.get('deadline'))).toISOString(),minutes:Number(f.get('minutes')),priority:Number(f.get('priority'))}))}>
          <select name="kind" aria-label="计划类型"><option value="assessment">考试备考</option><option value="task">学习任务</option></select>
          <input name="title" aria-label="任务名称" placeholder="名称" required/><label>截止时间<input name="deadline" type="datetime-local" required/></label>
          <label>预计分钟<input name="minutes" type="number" min="5" max="10000" defaultValue="60" required/></label><label>优先级（1–5）<input name="priority" type="number" min="1" max="5" defaultValue="2" required/></label><button>生成计划</button>
        </form>}
      </article>
      <article><h3>可用时间</h3><p>按截止时间与优先级排程，自动拆分长任务；冲突会保留提示。</p>
        <form onSubmit={e => submit(e, f => ({action:'availability',slots:[...state.availability,{start:new Date(String(f.get('start'))).toISOString(),end:new Date(String(f.get('end'))).toISOString()}]}))}>
          <label>开始<input type="datetime-local" name="start" required/></label><label>结束<input type="datetime-local" name="end" required/></label><button>添加时段并重排</button></form>
        {state.availability.map((slot,i) => <p key={slot.start}>{localDate(slot.start)} — {localDate(slot.end)} <button onClick={() => act({action:'availability',slots:state.availability.filter((_,j)=>i!==j)})}>移除</button></p>)}
        <button onClick={() => act({action:'replan'})}>重新排程</button>
        <button onClick={async () => {try {const d=await request('ics'); const url=URL.createObjectURL(new Blob([d.ics],{type:'text/calendar;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download='StudyCrew.ics';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}catch(e){setError(String(e));}}}>导出日历 ICS</button>
        <button onClick={async () => {if(notifications){await act({action:'reminder_settings',enabled:false});return;}if(window.knowe?.learningNotificationSupported){if(!await window.knowe.learningNotificationSupported()){setError('当前环境不支持桌面通知');return;}await act({action:'reminder_settings',enabled:true});return;}if (!('Notification' in window)) {setError('当前环境不支持系统通知');return;}if(await Notification.requestPermission()==='granted')await act({action:'reminder_settings',enabled:true});else setError('系统未允许通知');}}>{notifications?'关闭系统提醒':'开启系统提醒'}</button><small>桌面提醒在应用或托盘运行时生效；完整退出后不发送。浏览器提醒需保持学习页打开。{notifications ? '已启用。' : ''}</small>
      </article>
      <article><h3>待办与冲突</h3>{state.conflicts.map(c => <p role="status" key={c.task_id}>「{state.tasks.find(t=>t.id===c.task_id)?.title}」缺少 {c.unallocated_minutes} 分钟可用时间。</p>)}
        {state.tasks.map(t => <div className="learning-task" key={t.id}><strong>{t.title}</strong> <span>{t.status} · {t.minutes} 分钟</span>{t.sessions.map(x => <p key={x.start}>{localDate(x.start)} — {localDate(x.end)}</p>)}
          {t.status==='pending' && <><button onClick={()=>act({action:'complete',task_id:t.id})}>完成</button><button onClick={()=>act({action:'skip',task_id:t.id})}>跳过并重排</button><form onSubmit={e=>submit(e,f=>({action:'postpone',task_id:t.id,not_before:new Date(String(f.get('after'))).toISOString()}))}><input type="datetime-local" name="after" aria-label="延期到" required/><button>延期并重排</button></form></>}
          {t.status==='pending' && <details><summary>修改任务和截止时间</summary><form onSubmit={e=>submit(e,f=>({action:'edit_task',task_id:t.id,title:f.get('title'),minutes:Number(f.get('minutes')),priority:Number(f.get('priority')),deadline:new Date(String(f.get('deadline'))).toISOString()}))}><input name="title" aria-label="修改任务名称" defaultValue={t.title} required/><input name="minutes" aria-label="修改任务分钟" type="number" min="5" max="10000" defaultValue={t.minutes} required/><input name="priority" aria-label="修改任务优先级" type="number" min="1" max="5" defaultValue="2" required/><input name="deadline" aria-label="修改任务截止时间" type="datetime-local" required/><button>保存并重排</button></form></details>}
          {t.status==='completed' && <form onSubmit={e=>submit(e,f=>({action:'feedback',task_id:t.id,rating:Number(f.get('rating')),note:f.get('note')}))}><select name="rating" aria-label="学习自评"><option value="1">仍需帮助</option><option value="2">较难</option><option value="3">掌握</option><option value="4">很轻松</option></select><input name="note" aria-label="学习反馈" placeholder="学习反馈（自评，不自动推断掌握度）" required/><button>记录反馈</button></form>}
        </div>)}
      </article>
      <article><h3>练习与间隔复习</h3><p>根据掌握度选择难度相近的题目。当前题库使用确定性答案比对。</p>
        {courseId && <details><summary>录入练习题</summary><form onSubmit={e=>submit(e,f=>({action:'question',course_id:courseId,topic:f.get('topic'),prompt:f.get('prompt'),answer:f.get('answer'),explanation:f.get('explanation'),difficulty:Number(f.get('difficulty'))}))}>
          <input name="topic" placeholder="知识点" aria-label="知识点" required/><textarea name="prompt" placeholder="题目" aria-label="题目" required/><input name="answer" placeholder="标准答案" aria-label="标准答案" required/><textarea name="explanation" placeholder="解析" aria-label="解析" required/><label>难度<input name="difficulty" type="number" min="1" max="5" defaultValue="2"/></label><button>保存题目</button></form></details>}
        {question && <form onSubmit={e=>{e.preventDefault();void act({action:'practice',question_id:question.id,answer});setAnswer('');}}><p>{question.topic} · 难度 {question.difficulty}</p><strong>{question.prompt}</strong><input value={answer} onChange={e=>setAnswer(e.target.value)} aria-label="练习答案" required/><button>提交练习</button></form>}
        {state.attempts.slice(-1).map(a=><p key={a.id}>{a.correct?'回答正确':'需要复习'}：{a.explanation}</p>)}
        {mastery.map(m=><p key={m.topic}>{m.topic}：掌握度 {Math.round(m.score*100)}%，下次复习 {localDate(m.review_at)}</p>)}
      </article>
      <MemoryPanel key={`${selected}:${courseId}`} groupId={selected} courseId={courseId}/>
    </div><details><summary>只读学校演示与受控改进</summary><p>学校接口目前为明确标记的模拟数据：线性代数 / 演示测验；周一 09:00–10:00 演示课表；成绩未知；认证状态：演示、未登录。不会提交作业或写入学校系统。</p><button onClick={async()=>{const token=generation.current;try{const d=await request('state',undefined,'&include_ranking=true');if(token===generation.current)setError(d.school.ranking?.position == null?'模拟排名：未知（未进行比较）':`模拟排名：${d.school.ranking.position}`);}catch(e){if(token===generation.current)setError(String(e));}}}>查看模拟排名</button>
      <form onSubmit={e=>submit(e,f=>({action:'candidate',kind:f.get('kind'),text:f.get('text')}))}><select name="kind" aria-label="改进类型"><option value="learning_method">学习方法</option><option value="prompt">提示词</option><option value="retrieval">检索</option><option value="routing">路由</option><option value="agent_template">Agent 模板</option></select><textarea name="text" aria-label="改进建议" required/><button>创建待评审候选</button></form>
      {state.candidates.map(c=><div className="learning-task" key={c.id}><p>{c.text} · {c.status}</p><form onSubmit={e=>submit(e,f=>({action:'evaluate_candidate',candidate_id:c.id,evaluation:{golden_pass:f.get('golden')==='on',permission_pass:f.get('permission')==='on',shadow_pass:f.get('shadow')==='on',report:f.get('report')}}))}><label><input name="golden" type="checkbox"/>黄金集通过</label><label><input name="permission" type="checkbox"/>权限回归通过</label><label><input name="shadow" type="checkbox"/>影子对比通过</label><input name="report" aria-label="评测报告位置和结论" placeholder="评测报告位置和结论（人工录入）" required/><button>记录评测证据</button></form><button disabled={!c.evaluation} onClick={()=>act({action:'approve_candidate',candidate_id:c.id,confirmed:true})}>人工批准供手动应用</button><small>不会自动执行代码或变更权限。</small></div>)}
    </details></fieldset>}
  </section>;
}
