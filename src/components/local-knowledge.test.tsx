// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { Group } from '../contract/group';
import { useGroupStore } from '../store/groups';
import { fenceDeletedGroup } from '../store/groupDeletionFence';
import KnowledgeScopedView from './KnowledgeScopedView';

vi.mock('./KnowledgeView', () => ({ default: () => <div>旧知识资产视图</div> }));
const groupA = `grp_${'1a'.repeat(16)}`;
const groupB = `grp_${'2b'.repeat(16)}`;
const source = { source_id: `src_${'a'.repeat(32)}`, filename: '讲义.txt', status: 'indexed', chunk_count: 1 };
const hit = { ...source, chunk_id: 'chunk-1', location: 'para:1#1', text: '二次函数导数证据' };

function group(id: string, lifecycle: 'active' | 'archived' = 'active'): Group {
  return { group_id: id, schema_version: 1, name: id === groupA ? '数学组' : '英语组', template: 'custom', lifecycle,
    roster: [], budget: null, model_overrides: {}, tool_overrides: {}, source_links: [],
    course_bindings: [{ binding_id: `gcb_${'c'.repeat(32)}`, group_id: id, course_id: id === groupA ? 'calculus' : 'english', created_at: '2026-10-04T00:00:00Z' }],
    created_at: '2026-10-04T00:00:00Z', updated_at: '2026-10-04T00:00:00Z', archived_at: null, legacy_project_id: null };
}

function response(body: object) { return { ok: true, json: async () => ({ ok: true, mode: 'lexical', degraded: true, ...body }) } as Response; }
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>((done) => { resolve = done; }); return { promise, resolve }; }
function choose(id = groupA) { fireEvent.change(screen.getByLabelText('知识群组范围'), { target: { value: id } }); }

beforeEach(() => {
  useGroupStore.setState({ groups: { [groupA]: group(groupA), [groupB]: group(groupB) }, order: [groupA, groupB] });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('local knowledge center', () => {
  it('imports selected bytes within selected group/course, searches and inspects exact citation, confirms deletion', async () => {
    let stored = false;
    const calls: Array<{ url: URL; payload: Record<string, unknown> }> = [];
    vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
      const url = new URL(input); const payload = JSON.parse(String(init?.body || '{}'));
      calls.push({ url, payload });
      if (url.pathname.endsWith('/import')) { stored = true; return response({ source }); }
      if (url.pathname.endsWith('/search')) return response({ results: [hit] });
      if (url.pathname.endsWith('/detail')) return response({ source, chunks: [hit] });
      if (url.pathname.endsWith('/delete')) { stored = false; return response({ deleted: true }); }
      return response({ sources: stored ? [source] : [] });
    }));
    render(<KnowledgeScopedView />);
    expect(screen.queryByText('旧知识资产视图')).not.toBeInTheDocument();
    choose();
    expect(screen.getByText('旧知识资产视图')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('知识课程范围'), { target: { value: 'calculus' } });
    await waitFor(() => expect(screen.getByLabelText('导入本地资料')).not.toBeDisabled());
    fireEvent.change(screen.getByLabelText('导入本地资料'), { target: { files: [new File(['二次函数导数证据'], '讲义.txt', { type: 'text/plain' })] } });
    await screen.findByRole('button', { name: '讲义.txt' });
    const upload = calls.find((call) => call.url.pathname.endsWith('/import'))!;
    expect(upload.url.searchParams.get('group_id')).toBe(groupA);
    expect(upload.url.searchParams.get('course_id')).toBe('calculus');
    expect(upload.payload.filename).toBe('讲义.txt');
    expect(upload.payload.data_base64).toBeTruthy();
    expect(upload.payload).not.toHaveProperty('path');
    fireEvent.change(screen.getByLabelText('本地资料查询'), { target: { value: '导数' } });
    fireEvent.click(screen.getByRole('button', { name: '检索证据' }));
    const citation = await screen.findByRole('button', { name: '讲义.txt · para:1#1 · 查看引用文本' });
    fireEvent.click(citation);
    const inspection = await screen.findByLabelText('引用文本检查');
    expect(within(inspection).getByText('para:1#1')).toBeInTheDocument();
    expect(within(inspection).getByText('二次函数导数证据')).toBeInTheDocument();
    const detail = calls.find((call) => call.url.pathname.endsWith('/detail'))!;
    expect(detail.url.searchParams.get('chunk_id')).toBe('chunk-1');
    fireEvent.click(screen.getByLabelText('删除资料 讲义.txt'));
    expect(calls.some((call) => call.url.pathname.endsWith('/delete'))).toBe(false);
    fireEvent.click(screen.getByRole('button', { name: '确认删除资料' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: '讲义.txt' })).not.toBeInTheDocument());
    expect(screen.queryByLabelText('引用文本检查')).not.toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('fences delayed A → B → A responses by mount generation', async () => {
    const oldA = deferred<Response>();
    let seenA = 0;
    vi.stubGlobal('fetch', vi.fn((input: string) => {
      const id = new URL(input).searchParams.get('group_id');
      if (id === groupA && ++seenA === 1) return oldA.promise;
      return Promise.resolve(response({ sources: [{ ...source, filename: id === groupA ? '新数学.txt' : '英语.txt' }] }));
    }));
    render(<KnowledgeScopedView />);
    choose(); choose(groupB);
    await screen.findByRole('button', { name: '英语.txt' });
    choose();
    await screen.findByRole('button', { name: '新数学.txt' });
    await act(async () => { oldA.resolve(response({ sources: [{ ...source, filename: '旧数学泄漏.txt' }] })); });
    expect(screen.queryByText('旧数学泄漏.txt')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '英语.txt' })).not.toBeInTheDocument();
  });

  it('does not display a delayed search after scope switch and source detail after deletion', async () => {
    const pendingSearch = deferred<Response>();
    const pendingDetail = deferred<Response>();
    vi.stubGlobal('fetch', vi.fn((input: string) => {
      const url = new URL(input);
      if (url.pathname.endsWith('/search')) return pendingSearch.promise;
      if (url.pathname.endsWith('/detail')) return pendingDetail.promise;
      return Promise.resolve(response({ sources: url.searchParams.get('group_id') === groupA ? [source] : [] }));
    }));
    render(<KnowledgeScopedView />); choose();
    await screen.findByRole('button', { name: '讲义.txt' });
    fireEvent.change(screen.getByLabelText('本地资料查询'), { target: { value: '导数' } });
    fireEvent.click(screen.getByRole('button', { name: '检索证据' }));
    choose(groupB);
    await act(async () => pendingSearch.resolve(response({ results: [hit] })));
    expect(screen.queryByText(hit.text)).not.toBeInTheDocument();
    choose();
    fireEvent.click(await screen.findByRole('button', { name: '讲义.txt' }));
    act(() => {
      useGroupStore.setState({ groups: { [groupB]: group(groupB) }, order: [groupB] });
    });
    await act(async () => pendingDetail.resolve(response({ source, chunks: [hit] })));
    expect(screen.queryByLabelText('引用文本检查')).not.toBeInTheDocument();
    expect(screen.queryByText(hit.text)).not.toBeInTheDocument();
  });

  it('shows archive read-only and scan-only PDF state honestly', async () => {
    useGroupStore.setState({ groups: { [groupA]: group(groupA, 'archived') }, order: [groupA] });
    vi.stubGlobal('fetch', vi.fn(async () => response({ sources: [{ ...source, status: 'needs_vision', filename: 'scan.pdf', chunk_count: 0 }] })));
    render(<KnowledgeScopedView />); choose();
    await screen.findByRole('button', { name: 'scan.pdf' });
    expect(screen.getByLabelText('导入本地资料')).toBeDisabled();
    expect(screen.getByLabelText('删除资料 scan.pdf')).toBeDisabled();
    expect(screen.getByText(/含无文本层页面，需要 OCR/)).toBeInTheDocument();
    expect(screen.getByText(/本地关键词检索（BM25）/)).toBeInTheDocument();
  });

  it('clears course inspection when the selected course is unbound', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: string) => new URL(input).pathname.endsWith('/detail')
      ? response({ source, chunks: [hit] }) : response({ sources: [source] })));
    render(<KnowledgeScopedView />); choose();
    fireEvent.change(screen.getByLabelText('知识课程范围'), { target: { value: 'calculus' } });
    fireEvent.click(await screen.findByRole('button', { name: '讲义.txt' }));
    await screen.findByLabelText('引用文本检查');
    act(() => useGroupStore.setState({ groups: { [groupA]: { ...group(groupA), course_bindings: [] }, [groupB]: group(groupB) } }));
    expect(screen.queryByLabelText('引用文本检查')).not.toBeInTheDocument();
    expect(screen.getByLabelText('知识课程范围')).toHaveValue('');
  });

  it('does not publish responses after the renderer deletion fence', async () => {
    const doomedId = `grp_${'d3'.repeat(16)}`;
    useGroupStore.setState({ groups: { [doomedId]: group(doomedId) }, order: [doomedId] });
    const pending = deferred<Response>();
    vi.stubGlobal('fetch', vi.fn(() => pending.promise));
    render(<KnowledgeScopedView />); choose(doomedId);
    fenceDeletedGroup(doomedId);
    await act(async () => pending.resolve(response({ sources: [source] })));
    expect(screen.queryByRole('button', { name: '讲义.txt' })).not.toBeInTheDocument();
  });
});
