import { useEffect, useRef, useState } from 'react';
import { runtimeFetch } from '../shared/runtimeFetch';
import { runtimeHttpBase } from '../shared/runtimeEndpoints';
import { GroupSchema, type Group } from '../contract/group';
import { useGroupStore } from '../store/groups';
import { groupCommitAllowed, groupDeletionGeneration } from '../store/groupDeletionFence';
import './local-knowledge.css';

type Source = { source_id: string; filename: string; status: string; chunk_count: number };
type Evidence = { source_id: string; filename: string; chunk_id: string; location: string; text: string };
type Chunk = { chunk_id: string; location: string; text: string };
type SourceResponse = { sources: Source[]; group?: unknown };
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
  const [searched, setSearched] = useState(false);
  const [inspection, setInspection] = useState<{ source_id: string; filename: string; chunks: Chunk[] } | null>(null);
  const [query, setQuery] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
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
    setBusy(false);
  }), [groupId]);

  useEffect(() => {
    live.current = true;
    const token = ++epoch.current;
    setBusy(true);
    const expectedKey = associationKey(useGroupStore.getState().groups[groupId]);
    void request<SourceResponse>(scope, 'sources').then((body) => {
      syncCanonicalGroup(body.group, token, expectedKey);
      if (current(token)) setSources(body.sources.filter((row) => sourceAllowed(row.source_id)));
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
    catch { if (current(token)) setError('操作失败：请检查资料格式、群组/课程权限或加密存储。'); }
    finally { if (current(token)) setBusy(false); }
  }

  async function inspect(sourceId: string, chunkId?: string) {
    await run(async (token) => {
      const detailScope = `${scope}&source_id=${encodeURIComponent(sourceId)}${chunkId ? `&chunk_id=${encodeURIComponent(chunkId)}` : ''}`;
      const body = await request<{ source: Source; chunks: Chunk[] }>(detailScope, 'detail');
      if (current(token) && body.source.source_id === sourceId && sourceAllowed(sourceId)) {
        setInspection({ source_id: sourceId, filename: body.source.filename, chunks: body.chunks });
      }
    });
  }

  return <section className="local-knowledge-panel" aria-label="本地资料检索">
    <div className="local-knowledge-heading"><strong>本地资料与证据</strong>
      <small>本地关键词检索（BM25）；语义检索与 OCR 未启用。结果是资料证据，并非已核验答案。</small></div>
    <label className="local-knowledge-import">导入本地资料（最大 20 MB）
      <input aria-label="导入本地资料" type="file" accept=".txt,.md,.markdown,.html,.htm,.docx,.pptx,.xlsx,.pdf" disabled={busy || readOnly}
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
    {busy ? <p role="status">正在处理本地资料…</p> : null}
    {error ? <p role="alert">{error}</p> : null}
    <ul className="local-knowledge-sources" aria-label="本地资料列表">{sources.filter((row) => sourceAllowed(row.source_id)).map((source) => <li key={source.source_id}>
      <button disabled={busy} onClick={() => void inspect(source.source_id)}>{source.filename}</button>
      <small>{source.status === 'needs_vision' ? '含无文本层页面，需要 OCR/视觉解析（未启用）' : source.status === 'empty' ? '未抽取到文本' : `${source.chunk_count} 个文本片段`}</small>
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
