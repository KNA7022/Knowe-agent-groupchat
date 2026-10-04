import { useEffect, useRef, useState } from 'react';
import { runtimeFetch } from '../shared/runtimeFetch';
import { runtimeHttpBase } from '../shared/runtimeEndpoints';
import { useGroupStore } from '../store/groups';
import { groupCommitAllowed, groupDeletionGeneration } from '../store/groupDeletionFence';
import './local-knowledge.css';

type Source = { source_id: string; filename: string; status: string; chunk_count: number };
type Evidence = { source_id: string; filename: string; chunk_id: string; location: string; text: string };
type Chunk = { chunk_id: string; location: string; text: string };
const MAX_BYTES = 20 * 1024 * 1024;

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
  const [inspection, setInspection] = useState<{ filename: string; chunks: Chunk[] } | null>(null);
  const [query, setQuery] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [pendingDelete, setPendingDelete] = useState<Source | null>(null);
  const generation = useRef(groupDeletionGeneration(groupId));
  const epoch = useRef(0);
  const live = useRef(true);
  const scope = `?${new URLSearchParams({ group_id: groupId, ...(courseId ? { course_id: courseId } : {}) })}`;
  const current = (token: number) => {
    const group = useGroupStore.getState().groups[groupId];
    return live.current && token === epoch.current && groupCommitAllowed(groupId, generation.current)
      && group !== undefined && (!courseId || group.course_bindings.some((binding) => binding.course_id === courseId));
  };

  useEffect(() => {
    live.current = true;
    const token = ++epoch.current;
    setBusy(true);
    void request<{ sources: Source[] }>(scope, 'sources').then((body) => {
      if (current(token)) setSources(body.sources);
    }).catch(() => { if (current(token)) setError('本地资料暂时不可用，请检查后端与加密存储。'); })
      .finally(() => { if (current(token)) setBusy(false); });
    return () => { live.current = false; epoch.current += 1; };
    // Parent keys this panel by group + course; each mount owns a separate response generation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope]);

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
      if (current(token)) setInspection({ filename: body.source.filename, chunks: body.chunks });
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
            const data = await readFile(file);
            if (!current(token)) return;
            await request(scope, 'import', { filename: file.name, data_base64: data });
            const body = await request<{ sources: Source[] }>(scope, 'sources');
            if (current(token)) { setSources(body.sources); setResults([]); setSearched(false); setInspection(null); }
          });
        }} />
    </label>
    {readOnly ? <p>已归档群组：可检索与查看，不能导入或删除。</p> : null}
    {busy ? <p role="status">正在处理本地资料…</p> : null}
    {error ? <p role="alert">{error}</p> : null}
    <ul className="local-knowledge-sources" aria-label="本地资料列表">{sources.map((source) => <li key={source.source_id}>
      <button disabled={busy} onClick={() => void inspect(source.source_id)}>{source.filename}</button>
      <small>{source.status === 'needs_vision' ? '含无文本层页面，需要 OCR/视觉解析（未启用）' : source.status === 'empty' ? '未抽取到文本' : `${source.chunk_count} 个文本片段`}</small>
      <button aria-label={`删除资料 ${source.filename}`} disabled={busy || readOnly} onClick={() => setPendingDelete(source)}>删除</button>
    </li>)}</ul>
    {sources.length === 0 && !busy ? <p>此范围尚无本地资料。</p> : null}
    <form className="local-knowledge-search" onSubmit={(event) => {
      event.preventDefault();
      void run(async (token) => {
        const body = await request<{ results: Evidence[] }>(scope, 'search', { query });
        if (current(token)) { setResults(body.results); setSearched(true); setInspection(null); }
      });
    }}>
      <label>检索资料<input aria-label="本地资料查询" value={query} maxLength={1000} onChange={(event) => setQuery(event.target.value)} /></label>
      <button disabled={busy || !query.trim()} type="submit">检索证据</button>
    </form>
    <ul aria-label="本地检索结果">{results.map((hit) => <li key={hit.chunk_id}>
      <button disabled={busy} onClick={() => void inspect(hit.source_id, hit.chunk_id)}>{hit.filename} · {hit.location} · 查看引用文本</button>
      <p>{hit.text}</p>
    </li>)}</ul>
    {searched && results.length === 0 && !busy ? <p>未找到匹配的资料证据。</p> : null}
    {inspection ? <aside className="local-knowledge-inspection" aria-label="引用文本检查">
      <strong>{inspection.filename} · 抽取文本检查</strong><small>以下为存储的精确位置与文本片段。</small>
      <button onClick={() => setInspection(null)}>关闭文本检查</button>
      {inspection.chunks.map((chunk) => <div key={chunk.chunk_id}><code>{chunk.location}</code><pre>{chunk.text || '该位置没有可检索文本。'}</pre></div>)}
    </aside> : null}
    {pendingDelete ? <div role="dialog" aria-label="确认删除本地资料" className="local-knowledge-confirm">
      <p>删除“{pendingDelete.filename}”在此群组的关联？其他群组仍使用的资料会保留。</p>
      <button disabled={busy} onClick={() => void run(async (token) => {
        await request(scope, 'delete', { source_id: pendingDelete.source_id });
        const body = await request<{ sources: Source[] }>(scope, 'sources');
        if (current(token)) { setSources(body.sources); setResults([]); setSearched(false); setInspection(null); setPendingDelete(null); }
      })}>确认删除资料</button><button disabled={busy} onClick={() => setPendingDelete(null)}>取消删除</button>
    </div> : null}
  </section>;
}
