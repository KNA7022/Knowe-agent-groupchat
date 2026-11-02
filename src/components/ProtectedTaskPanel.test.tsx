import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ProtectedTaskPanel } from './ProtectedTaskPanel';

const mocks = vi.hoisted(() => ({fetch: vi.fn()}));
vi.mock('../shared/runtimeFetch', () => ({runtimeFetch: mocks.fetch}));
vi.mock('../shared/runtimeEndpoints', () => ({runtimeHttpBase: () => 'http://local'}));
beforeEach(() => {mocks.fetch.mockReset();});
afterEach(() => {cleanup();});
const card = {approval_id: 'apr_demo', generation: 7, digest: 'exact-displayed-digest', status: 'pending',
  expires_at: '2026-10-11T01:00:00Z', proposal: {target_role: 'planning', instruction: '具体任务指令'}};
const snapshot = {ok: true, cards: [card], runs: [], messages: []};

describe('ProtectedTaskPanel', () => {
  it('approves the displayed version and scope without publishing input to legacy chat', async () => {
    mocks.fetch.mockResolvedValue({ok: true, json: async () => snapshot});
    render(<ProtectedTaskPanel groupId="grp_one" />);
    await screen.findByText('具体任务指令');
    fireEvent.click(screen.getByText('批准并执行此版本'));
    await waitFor(() => expect(mocks.fetch).toHaveBeenCalledWith('http://local/task-runtime/protected-resolve', expect.anything()));
    const invocation = mocks.fetch.mock.calls.find(([url]) => String(url).endsWith('protected-resolve'));
    expect(JSON.parse(invocation![1].body)).toMatchObject({group_id: 'grp_one', approval_id: 'apr_demo', generation: 7,
      digest: 'exact-displayed-digest', approved: true});
  });
  it('shows no-key failure and preserves unsent instruction for retry', async () => {
    mocks.fetch.mockImplementation(async (url: string) => String(url).endsWith('protected-propose')
      ? {ok: false, json: async () => ({ok: false, error: '加密密钥不可用'})}
      : {ok: true, json: async () => ({ok: true, cards: [], runs: [], messages: []})});
    render(<ProtectedTaskPanel groupId="grp_one" />);
    const input = screen.getByLabelText('任务指令');
    fireEvent.change(input, {target: {value: '保留此任务'}});
    fireEvent.click(screen.getByText('生成审批提案'));
    expect(await screen.findByRole('alert')).toHaveTextContent('加密密钥不可用');
    expect(input).toHaveValue('保留此任务');
  });
});
