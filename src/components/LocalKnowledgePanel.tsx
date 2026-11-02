import { useEffect, useRef, useState } from 'react';
import { runtimeFetch } from '../shared/runtimeFetch';
import { runtimeHttpBase } from '../shared/runtimeEndpoints';
import { GroupSchema, type Group } from '../contract/group';
import { useGroupStore } from '../store/groups';
import { groupCommitAllowed, groupDeletionGeneration } from '../store/groupDeletionFence';
import './local-knowledge.css';

type Source = { source_id: string; filename: string; status: string; chunk_count: number; version: number; visual_support?: string };
type Evidence = { source_id: string; filename: string; chunk_id: string; location: string; text: string };
type Chunk = { chunk_id: string; location: string; text: string };
type SourceResponse = { sources: Source[]; group?: unknown; pending_operations?: RagPending[] };
type RagPending = { operation_id: string; approval_id: string; group_id: string; run_id: string; summary: string; capabilities: string[]; route: string; payload: Record<string, unknown> };
class RagApprovalError extends Error {
  constructor(public pending: RagPending) { super('rag_approval_required'); }
}
const MAX_BYTES = 20 * 1024 * 1024;

function associationKey(group: Group | undefined): string {
  return group?.source_links.map((link) => `${link.link_id}:${link.source_id}`).sort().join('|') || '';
}

async function request<T>(scope: string, route: string, payload?: unknown): Promise<T> {
  const response = await runtimeFetch(`${runtimeHttpBase()}/api/knowledge/local/${route}${scope}`, {
    method: payload === undefined ? 'GET' : 'POST', headers: { 'Content-Type': 'application/json' },
    body: payload === undefined ? undefined : JSON.stringify(payload),
  });
  const body = await response.json();
  if (response.status === 202 && body.approval_required) throw new RagApprovalError({ ...body, route, payload: payload as Record<string, unknown> });
  if (!response.ok || body.ok === false) throw new Error(body.error || 'local_knowledge_unavailable');
  return body as T;
}

function readFile(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(',')[1] || '');
    reader.onerror = () => reject(new Error('file_read_failed'));
    reader.readAsDataURL(file);
  });
}

export default function LocalKnowledgePanel({ groupId, courseId, readOnly }: {
  groupId: string; courseId: string; readOnly: boolean;
}) {
  const [sources, setSources] = useState<Source[]>([]);
  const [results, setResults] = useState<Evidence[]>([]);
  const [answer, setAnswer] = useState<{ answer: string; verification: string; citations: (Evidence & { version: number; quote: string })[] } | null>(null);
  const [searched, setSearched] = useState(false);
  const [inspection, setInspection] = useState<{ source_id: string; filename: string; chunks: Chunk[] } | null>(null);
  const [query, setQuery] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [pendingApproval, setPendingApproval] = useState<RagPending | null>(null);
  const [pendingDelete, setPendingDelete] = useState<Source | null>(null);
  const generation = useRef(groupDeletionGeneration(groupId));
  const epoch = useRef(0);
  const live = useRef(true);
  const associations = useGroupStore((state) => associationKey(state.groups[groupId]));
  const scope = `?${new URLSearchParams({ group_id: groupId, ...(courseId ? { course_id: courseId } : {}) })}`;
  const current = (token: number) => {
    const group = useGroupStore.getState().groups[groupId];
    return live.current && token === epoch.current && groupCommitAllowed(groupId, generation.current)
      && group !== undefined && (!courseId || group.course_bindings.some((binding) => binding.course_id === courseId));
  };
  const sourceAllowed = (sourceId: string) => Boolean(useGroupStore.getState().groups[groupId]?.source_links
    .some((link) => link.source_id === sourceId));

  function syncCanonicalGroup(raw: unknown, token: number, expectedKey: string): void {
    if (!raw || !current(token)) return;
    const canonical = GroupSchema.parse(raw);
    if (canonical.group_id !== groupId) throw new Error('group_response_mismatch');
    // A management unlink which wins while this response is in flight must never be overwritten.
    useGroupStore.setState((state) => {
      const existing = state.groups[groupId];
      if (!current(token) || associationKey(existing) !== expectedKey || !existing
          || associationKey(canonical) === expectedKey) return state;
      const allowed = new Set(canonical.source_links.map((link) => link.source_id));
      return { groups: { ...state.groups, [groupId]: { ...existing, source_links: canonical.source_links } },
        sourceReferenceCounts: { ...state.sourceReferenceCounts,
          [groupId]: Object.fromEntries(Object.entries(state.sourceReferenceCounts[groupId] || {}).filter(([id]) => allowed.has(id))) } };
    });
  }

  useEffect(() => useGroupStore.subscribe((state, previous) => {
    if (associationKey(state.groups[groupId]) === associationKey(previous.groups[groupId])) return;
    // Advance synchronously, before a queued detail/search response can commit.
    epoch.current += 1;
    const allowed = new Set(state.groups[groupId]?.source_links.map((link) => link.source_id) || []);
    setSources((rows) => rows.filter((row) => allowed.has(row.source_id)));
    setResults((rows) => rows.filter((row) => allowed.has(row.source_id)));
    setInspection((value) => value && allowed.has(value.source_id) ? value : null);
    setPendingDelete((value) => value && allowed.has(value.source_id) ? value : null);
    setPendingApproval(null);
    setAnswer(null);
    setBusy(false);
  }), [groupId]);

  useEffect(() => {
    live.current = true;
    const token = ++epoch.current;
    setBusy(true);
    const expectedKey = associationKey(useGroupStore.getState().groups[groupId]);
    void request<SourceResponse>(scope, 'sources').then((body) => {
      syncCanonicalGroup(body.group, token, expectedKey);
      if (current(token)) { setSources(body.sources.filter((row) => sourceAllowed(row.source_id))); setPendingApproval(body.pending_operations?.[0] || null); }
    }).catch(() => { if (current(token)) setError('本地资料暂时不可用，请检查后端与加密存储。'); })
      .finally(() => { if (current(token)) setBusy(false); });
    return () => { live.current = false; epoch.current += 1; };
    // Parent keys this panel by group + course; each mount owns a separate response generation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope, associations]);

  async function run(operation: (token: number) => Promise<void>) {
    const token = ++epoch.current;
    setBusy(true); setError('');
    try { await operation(token); }
    catch (failure) { if (current(token)) {
      if (failure instanceof RagApprovalError) setPendingApproval(failure.pending);
      else setError('操作失败：请检查模型/凭据配置、资料格式、群组/课程权限或加密存储。');
    } }
    finally { if (current(token)) setBusy(false); }
  }

  async function inspect(sourceId: string, chunkId?: string, version?: number) {
    await run(async (token) => {
      const detailScope = `${scope}&source_id=${encodeURIComponent(sourceId)}${chunkId ? `&chunk_id=${encodeURIComponent(chunkId)}` : ''}${version ? `&source_version=${version}` : ''}`;
      const body = await request<{ source: Source; chunks: Chunk[] }>(detailScope, 'detail');
      if (current(token) && body.source.source_id === sourceId && sourceAllowed(sourceId)) {
        setInspection({ source_id: sourceId, filename: body.source.filename, chunks: body.chunks });
      }
    });
  }

  return <section className="local-knowledge-panel" aria-label="本地资料检索">
    <div className="local-knowledge-heading"><strong>本地资料与证据</strong>
      <small>本地关键词检索（BM25）；答案以原文摘录生成并核对引用。扫描页使用授权的多模态模型读取。</small></div>
    <label className="local-knowledge-import">导入本地资料（最大 20 MB）
      <input aria-label="导入本地资料" type="file" accept=".txt,.md,.markdown,.html,.htm,.docx,.pptx,.xlsx,.pdf,.png,.jpg,.jpeg" disabled={busy || readOnly}
        onChange={(event) => {
          const file = event.target.files?.[0]; event.target.value = '';
          if (!file) return;
          if (file.size > MAX_BYTES) { setError('文件超过 20 MB 上限。'); return; }
          void run(async (token) => {
            const expectedKey = associationKey(useGroupStore.getState().groups[groupId]);
            const data = await readFile(file);
            if (!current(token)) return;
            const imported = await request<{ group?: unknown }>(scope, 'import', { filename: file.name, data_base64: data });
            if (!current(token)) return;
            setResults([]); setSearched(false); setInspection(null);
            syncCanonicalGroup(imported.group, token, expectedKey);
            if (!current(token)) return; // Source changes trigger a new fenced list read.
            const body = await request<SourceResponse>(scope, 'sources');
            if (current(token)) { setSources(body.sources.filter((row) => sourceAllowed(row.source_id))); setResults([]); setSearched(false); setInspection(null); }
          });
        }} />
    </label>
    {readOnly ? <p>已归档群组：可检索与查看，不能导入或删除。</p> : null}
    {pendingApproval && <aside role="dialog" aria-label="资料模型调用审批">
      <p>将当前资料证据发送给配置的模型，可能产生费用。审批绑定原文版本与本次请求，每次最多 2048 输出 token。</p>
      <button disabled={busy || readOnly} onClick={() => void run(async (token) => {
        const pending = pendingApproval;
        const approved = await runtimeFetch(`${runtimeHttpBase()}/task-runtime/approval`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ group_id: groupId, approval_id: pending.approval_id, approved: true, capabilities: pending.capabilities, request_key: `rag-approve:${pending.operation_id}` }) });
        if (!approved.ok) throw new Error('approval_failed');
        if (!current(token)) return;
        const result = await request<NonNullable<typeof answer>>(scope, pending.route, { ...pending.payload, authorization: { operation_id: pending.operation_id } });
        if (current(token)) {
          setPendingApproval(null);
          if (pending.route === 'answer') setAnswer(result);
          else {
            const body = await request<SourceResponse>(scope, 'sources');
            if (current(token)) setSources(body.sources.filter((row) => sourceAllowed(row.source_id)));
          }
        }
      })}>批准并执行此请求</button>
      <button disabled={busy} onClick={() => void run(async (token) => {
        const denied = await runtimeFetch(`${runtimeHttpBase()}/task-runtime/approval`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ group_id: groupId, approval_id: pendingApproval.approval_id, approved: false, capabilities: [], request_key: `rag-deny:${pendingApproval.operation_id}` }) });
        if (!denied.ok) throw new Error('approval_failed');
        if (current(token)) setPendingApproval(null);
      })}>拒绝</button>
    </aside>}
    {busy ? <p role="status">正在处理本地资料…</p> : null}
    {error ? <p role="alert">{error}</p> : null}
    <ul className="local-knowledge-sources" aria-label="本地资料列表">{sources.filter((row) => sourceAllowed(row.source_id)).map((source) => <li key={source.source_id}>
      <button disabled={busy} onClick={() => void inspect(source.source_id)}>{source.filename}</button>
      <small>{source.status === 'needs_vision' ? '含无文本层页面，等待授权多模态读取' : source.status === 'empty' ? '未抽取到文本' : `${source.chunk_count} 个文本片段`}</small>
      {(source.status === 'needs_vision' || source.visual_support === 'native_office_available') && <button disabled={busy || readOnly} onClick={() => {
        if (!window.confirm('将本资料最多 1 页发送给已配置的多模态模型读取，可能产生费用。是否授权？')) return;
        void run(async (token) => {
          await request(scope, 'vision', { source_id: source.source_id, source_version: source.version, approved: true, page_limit: 1 });
          const body = await request<SourceResponse>(scope, 'sources');
          if (current(token)) setSources(body.sources.filter((row) => sourceAllowed(row.source_id)));
        });
      }}>授权读取下一页</button>}
      {source.visual_support === 'native_office_available' && <small>可使用本地 Office 导出页面后授权视觉读取；文本索引不代表图表理解。</small>}
      {source.visual_support === 'unsupported_office_visuals' && <small>Office 当前仅抽取文本；图表、图片及版式尚不支持视觉理解。</small>}
      <button aria-label={`删除资料 ${source.filename}`} disabled={busy || readOnly} onClick={() => setPendingDelete(source)}>删除</button>
    </li>)}</ul>
    {sources.length === 0 && !busy ? <p>此范围尚无本地资料。</p> : null}
    <form className="local-knowledge-search" onSubmit={(event) => {
      event.preventDefault();
      void run(async (token) => {
        const body = await request<{ results: Evidence[] }>(scope, 'search', { query });
        if (current(token)) { setResults(body.results.filter((row) => sourceAllowed(row.source_id))); setSearched(true); setInspection(null); }
      });
    }}>
      <label>检索资料<input aria-label="本地资料查询" value={query} maxLength={1000} onChange={(event) => setQuery(event.target.value)} /></label>
      <button disabled={busy || !query.trim()} type="submit">检索证据</button>
    </form>
    <button disabled={busy || !query.trim()} onClick={() => void run(async (token) => {
      const body = await request<NonNullable<typeof answer>>(scope, 'answer', { query });
      if (current(token)) setAnswer(body);
    })}>依据资料生成摘录答案</button>
    <button disabled={busy || readOnly || !query.trim()} onClick={() => void run(async (token) => {
      const body = await request<NonNullable<typeof answer>>(scope, 'answer', { query, use_model: true, approved: true });
      if (current(token)) setAnswer(body);
    })}>申请模型依据资料回答</button>
    {answer && <aside aria-label="资料答案"><p>{answer.answer}</p><small>{answer.verification === 'exact_quotes_verified' ? '引用已逐字核对；未验证语义推断。' : '证据不足或引用核验失败。'}</small>
      {answer.citations.map((citation) => <button key={citation.chunk_id} onClick={() => void inspect(citation.source_id, citation.chunk_id, citation.version)}>{citation.location} · v{citation.version} · 查看原文</button>)}
    </aside>}
    <ul aria-label="本地检索结果">{results.filter((row) => sourceAllowed(row.source_id)).map((hit) => <li key={hit.chunk_id}>
      <button disabled={busy} onClick={() => void inspect(hit.source_id, hit.chunk_id)}>{hit.filename} · {hit.location} · 查看引用文本</button>
      <p>{hit.text}</p>
    </li>)}</ul>
    {searched && results.length === 0 && !busy ? <p>未找到匹配的资料证据。</p> : null}
    {inspection && sourceAllowed(inspection.source_id) ? <aside className="local-knowledge-inspection" aria-label="引用文本检查">
      <strong>{inspection.filename} · 抽取文本检查</strong><small>以下为存储的精确位置与文本片段。</small>
      <button onClick={() => setInspection(null)}>关闭文本检查</button>
      {inspection.chunks.map((chunk) => <div key={chunk.chunk_id}><code>{chunk.location}</code><pre>{chunk.text || '该位置没有可检索文本。'}</pre></div>)}
    </aside> : null}
    {pendingDelete && sourceAllowed(pendingDelete.source_id) ? <div role="dialog" aria-label="确认删除本地资料" className="local-knowledge-confirm">
      <p>删除“{pendingDelete.filename}”在此群组的关联？其他群组仍使用的资料会保留。</p>
      <button disabled={busy} onClick={() => void run(async (token) => {
        const expectedKey = associationKey(useGroupStore.getState().groups[groupId]);
        const deleted = await request<{ group?: unknown }>(scope, 'delete', { source_id: pendingDelete.source_id });
        syncCanonicalGroup(deleted.group, token, expectedKey);
        if (!current(token)) return;
        const body = await request<SourceResponse>(scope, 'sources');
        if (current(token)) { setSources(body.sources.filter((row) => sourceAllowed(row.source_id))); setResults([]); setSearched(false); setInspection(null); setPendingDelete(null); }
      })}>确认删除资料</button><button disabled={busy} onClick={() => setPendingDelete(null)}>取消删除</button>
    </div> : null}
  </section>;
}
