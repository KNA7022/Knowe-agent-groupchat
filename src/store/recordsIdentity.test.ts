import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { type HistoryItem, useRecordsStore } from './records';

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200, headers: { 'Content-Type': 'application/json' },
  });
}

function marker(project: string, seq = 1): Record<string, unknown> {
  return {
    seq, type: 'message', agent_id: 'agent', content: `${project}-BODY`, ts: null,
    has_files: false, has_images: false, has_videos: false, has_links: false,
  };
}

describe('records singleton request identity', () => {
  beforeEach(() => useRecordsStore.getState().reset());
  afterEach(() => { vi.unstubAllGlobals(); });

  it('keeps the latest Group load when A succeeds after B', async () => {
    const pending = new Map<string, (response: Response) => void>();
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const project = new URL(String(input)).searchParams.get('project_id')!;
      return new Promise<Response>((resolve) => pending.set(project, resolve));
    }));
    const a = useRecordsStore.getState().load('records-a');
    const b = useRecordsStore.getState().load('records-b');
    await vi.waitFor(() => expect(pending.size).toBe(2));
    pending.get('records-b')!(json({ items: [marker('B')], total: 1 }));
    await b;
    pending.get('records-a')!(json({ items: [marker('A')], total: 1 }));
    await a;
    expect(useRecordsStore.getState().items[0]?.content).toBe('B-BODY');
  });

  it('drops A loadMore success/error after switching to B', async () => {
    const pending: Array<{ project: string; resolve: (response: Response) => void; reject: (error: Error) => void }> = [];
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const project = new URL(String(input)).searchParams.get('project_id')!;
      return new Promise<Response>((resolve, reject) => pending.push({ project, resolve, reject }));
    }));
    useRecordsStore.setState({
      items: [marker('A') as unknown as HistoryItem], total: 2, page: 1, loading: false,
    });
    const moreA = useRecordsStore.getState().loadMore('records-more-a');
    const loadB = useRecordsStore.getState().load('records-more-b');
    await vi.waitFor(() => expect(pending).toHaveLength(2));
    pending.find((row) => row.project === 'records-more-b')!.resolve(json({ items: [marker('B')], total: 1 }));
    await loadB;
    pending.find((row) => row.project === 'records-more-a')!.reject(new Error('A late error'));
    await moreA;
    expect(useRecordsStore.getState().items[0]?.content).toBe('B-BODY');
    expect(useRecordsStore.getState().error).toBeNull();
  });

  it('keeps B handoffs when A resolves last', async () => {
    const pending = new Map<string, (response: Response) => void>();
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const project = new URL(String(input)).searchParams.get('project_id')!;
      return new Promise<Response>((resolve) => pending.set(project, resolve));
    }));
    const a = useRecordsStore.getState().loadHandoffs('records-handoff-a');
    const b = useRecordsStore.getState().loadHandoffs('records-handoff-b');
    await vi.waitFor(() => expect(pending.size).toBe(2));
    pending.get('records-handoff-b')!(json({ items: [{ id: 'b', report: 'B-HANDOFF' }] }));
    await b;
    pending.get('records-handoff-a')!(json({ items: [{ id: 'a', report: 'A-HANDOFF' }] }));
    await a;
    expect(useRecordsStore.getState().handoffs[0]?.report).toBe('B-HANDOFF');
  });
});
