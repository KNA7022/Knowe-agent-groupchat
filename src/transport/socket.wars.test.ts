/**
 * socket.wars.test.ts — 战史回归（v0.3 全量重建）
 *
 * 每一条 = 一场真打过的败仗。删任何一条之前，先解释为什么那场仗不会再来。
 *
 * ★ 与旧版最大的不同：**不 mock WebSocket 了。**
 *   旧版用 vi.fn() 捏了一个假的 WebSocket，于是测的是「我以为传输层会怎么做」，
 *   而不是「传输层真的会怎么做」——mock 和真实的差异正是 bug 的藏身处。
 *   现在跑的是真 socket.ts + 真 WebSocket + 真 FakeKnoweServer（真 TCP）。
 *
 * W1  纪元锁死    server 重启 → last_seq < 水位 → 清会话 + 拉快照
 * W2  静默丢弃    任何 dedup/gap 丢弃都必须在走廊留痕
 * W3  心跳泄漏    反复重连后不会攒出一堆 ping 定时器
 * W4  相互驱逐    close 4001 → 让位，状态 closed，不重连
 * W5  seq 空洞    gap → request_snapshot（按项目去抖）
 * W6  字段漂移    畸形事件 → Zod 拒收 → 走廊计数 +1，不进 store
 * W7  广播失聪    5s 没等到 user_echo → 哨兵告警
 * W8  握手乱序    实时事件先于回放到达 → 缓冲排序，历史零丢失
 * W9  跨项目污染  B 的事件不影响 A 的水位
 * W10 双解决      timeout + cancelled 先后到达 → 首个解决为准
 * W11 出站失败    未连接时发消息 → 响亮失败，绝不静默吞掉
 * W12 白名单旁路  pong / project_created 不参与水位
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createSocket, _resetGlobalSeen, type SocketAPI, type ConnStatus } from './socket';
import type { InboundEvent } from '../contract/envelope';
import { FakeKnoweServer, sleep } from '../fakeserver/FakeKnoweServer';
import {
  getCorridorState, resetCorridor, record as recordDiagnostic,
} from '../observe/corridor';
import { taskRunFixture } from '../contract/taskRuntime.fixture';
import { useKnoweStore } from '../store/store';
import { useTaskRuntimeStore } from '../store/taskRuntime';
import type { GroupId } from '../contract/group';

// jsdom 没有 WebSocket，把 Node 的 ws 顶上去（这样测的就是真连接）
import WS from 'ws';
(globalThis as unknown as { WebSocket: unknown }).WebSocket = WS;

// ═══════════════════════════════════════════════════════════════
// 夹具
// ═══════════════════════════════════════════════════════════════

let server: FakeKnoweServer;
let socket: SocketAPI | null = null;

interface Harness {
  socket: SocketAPI;
  events: InboundEvent[];
  statuses: ConnStatus[];
  epochResets: string[];
  echoLost: string[];
  ledger: Array<{ pid: string; activity: unknown[] }>;
  runtimeSnapshots: Array<{ groupId: string; snapshot: unknown }>;
}

function connect(opts: { ringMax?: number; projectRuntime?: boolean } = {}): Harness {
  const events: InboundEvent[] = [];
  const statuses: ConnStatus[] = [];
  const epochResets: string[] = [];
  const echoLost: string[] = [];
  const ledger: Array<{ pid: string; activity: unknown[] }> = [];
  const runtimeSnapshots: Array<{ groupId: string; snapshot: unknown }> = [];

  const s = createSocket({
    url: server.url,
    callbacks: {
      onEvent: (e) => events.push(e),
      onStatus: (st) => statuses.push(st),
      onEpochReset: (pid) => epochResets.push(pid),
      onEchoLost: (cmid, pid) => {
        echoLost.push(cmid);
        recordDiagnostic({ dir: 'out', type: 'user_message', projectId: pid,
          verdict: 'sentinel', summary: '回声超时' });
      },
      getActiveProjectId: () => 'demo',
      onDiagnostic: recordDiagnostic,     // ★ 走廊接上真传输层
      onActivityLedger: (pid, activity) => ledger.push({ pid, activity }), // [v1.0.24.4]
      onTaskRuntimeSnapshot: (groupId, snapshot) => {
        if (opts.projectRuntime) {
          useTaskRuntimeStore.getState().ingestGroupSnapshot(groupId as GroupId, snapshot);
        }
        runtimeSnapshots.push({ groupId, snapshot });
      },
    },
  });
  socket = s;
  s.connect();
  return { socket: s, events, statuses, epochResets, echoLost, ledger, runtimeSnapshots };
}

/** 等到条件成立，或超时炸掉（带上现场，方便查） */
async function until(cond: () => boolean, label: string, timeoutMs = 3000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (cond()) return;
    await sleep(15);
  }
  throw new Error(`超时：${label}`);
}

beforeEach(async () => {
  resetCorridor();
  _resetGlobalSeen(); // [v1.0.23.2] 用例间清全局去重（各用例复用相同 seq）
  server = new FakeKnoweServer({ ringMax: 5 });
  await server.start();
});

afterEach(async () => {
  socket?.disconnect();
  socket = null;
  await server.stop();
  vi.useRealTimers();
});

// ═══════════════════════════════════════════════════════════════

describe('W1 · 纪元锁死（server 重启）', () => {
  it('last_seq < 本地水位 → 清会话 + 拉快照，不是无脑覆盖水位', async () => {
    const h = connect();
    await until(() => h.statuses.includes('live'), '首次 live');

    server.say('demo', 'coordinator', '旧世界的消息', 4);
    await until(() => h.events.some((e) => e.type === 'message'), '收到旧消息');
    const oldWatermark = h.socket.watermarks.demo!;
    expect(oldWatermark).toBeGreaterThan(1);

    // server 重启：seq 归零、ring 清空
    server.restart();
    await until(() => h.statuses.at(-1) !== 'live', '断线');
    await until(() => h.epochResets.includes('demo'), '★ 纪元重置被识别', 6000);

    // replay_complete 本身就是权威快照/水位交接；不再依赖额外 HTTP/WS 快照。
    await until(() => h.statuses.at(-1) === 'live', '新纪元权威交接完成');
    expect(server.inbound.some(
      (m) => m.type === 'request_snapshot' && m.project_id === 'demo',
    )).toBe(false);
    // 关键是水位远低于旧水位——旧纪元的水位没有被带进新纪元。
    expect(h.socket.watermarks.demo!).toBeLessThan(oldWatermark);
    expect(getCorridorState().epochResets).toBeGreaterThan(0);   // 走廊留痕
  });

  it('真实 store 按新纪元顺序重建项目、花名册、活动、会话和 Runtime', async () => {
    const groupId = `grp_${'1'.repeat(32)}` as GroupId;
    const oldRunId = `run_${'a'.repeat(32)}`;
    const newRunId = `run_${'b'.repeat(32)}`;
    const runtimeSnapshot = (runId: string, title: string) => ({
      schema_version: 1,
      group_id: groupId,
      runs: [taskRunFixture({
        run_id: runId, task_id: `task_${runId.slice(4)}`, group_id: groupId,
        title, last_seq: 1, version: 1,
      })],
      pending_approvals: [],
      summary: {
        active_primary_run_id: runId, primary_task_status: 'running', queued_task_count: 0,
        active_side_task_count: 0, pending_approval_count: 0,
        budget_usage: {
          wall_clock_seconds: 0, model_turns: 0, tool_calls: 0, tokens: 0, cost_usd: 0,
        },
        indexing_state: 'idle',
      },
    });

    useKnoweStore.getState().clearProject('demo');
    useTaskRuntimeStore.getState().resetAll();
    const statuses: ConnStatus[] = [];
    const s = createSocket({
      url: server.url,
      callbacks: {
        onEvent: (event) => {
          if (event.type === 'task_runtime_event') {
            useTaskRuntimeStore.getState().applyEvent(event.group_id as GroupId, event.event);
          } else {
            useKnoweStore.getState().handleEvent(event);
          }
        },
        onStatus: (status) => {
          statuses.push(status);
          useKnoweStore.getState().setConnStatus(status);
        },
        onEpochReset: (pid) => useKnoweStore.getState().clearProject(pid),
        onProjectDiscovered: (pid, name) => useKnoweStore.getState().ensureProject(pid, name),
        onActivityLedger: (pid, activity) =>
          useKnoweStore.getState().calibrateActivity(pid, activity),
        onTaskRuntimeEpochReset: () => useTaskRuntimeStore.getState().resetAll(),
        onTaskRuntimeSnapshot: (gid, snapshot) =>
          useTaskRuntimeStore.getState().ingestGroupSnapshot(gid as GroupId, snapshot),
        getActiveProjectId: () => 'demo',
      },
    });
    socket = s;
    s.connect();
    await until(() => statuses.at(-1) === 'live', 'old epoch live');
    server.emitNoSeq({
      type: 'project_created', project_id: 'demo', project_name: 'Old project',
      members: [{ id: 'old_agent', role: 'Coach', name: 'Old member' }],
    });
    server.say('demo', 'old_agent', 'old-world', 2);
    await until(
      () => useKnoweStore.getState().convs.demo?.items.some(
        (item) => item.kind === 'agent' && item.text === 'old-world',
      ) === true,
      'old epoch projected',
    );
    useTaskRuntimeStore.getState().ingestGroupSnapshot(
      groupId, runtimeSnapshot(oldRunId, 'Old runtime'),
    );

    server.replayProjectCreated = [{
      type: 'project_created', project_id: 'demo', project_name: 'New project',
      members: [{ id: 'new_agent', role: 'Coach', name: 'New member' }],
    }];
    server.replayActivity = [{
      agent_id: 'new_agent', scope_id: 'new-scope', channel_id: 'demo', started_at: 42,
    }];
    server.replayTaskRuntimeGroups = [runtimeSnapshot(newRunId, 'New runtime')];
    server.restart();
    server.emit('demo', { type: 'message', agent_id: 'new_agent', content: 'new-world' });

    await until(() => statuses.at(-1) === 'live', 'new epoch live', 6000);
    await until(
      () => useKnoweStore.getState().convs.demo?.items.some(
        (item) => item.kind === 'agent' && item.text === 'new-world',
      ) === true,
      'new conversation projected',
      6000,
    );
    const conversation = useKnoweStore.getState().convs.demo!;
    expect(conversation.projectName).toBe('New project');
    expect(conversation.members.some((member) => member.id === 'old_agent')).toBe(false);
    expect(conversation.members.find((member) => member.id === 'new_agent')?.state).toBe('busy');
    expect(conversation.items.some(
      (item) => item.kind === 'agent' && item.text.startsWith('old-'),
    )).toBe(false);
    expect(useTaskRuntimeStore.getState().byRun[oldRunId]).toBeUndefined();
    expect(useTaskRuntimeStore.getState().byRun[newRunId]?.run.title).toBe('New runtime');

    server.emit('demo', {
      type: 'task_runtime_event', event_id: `evt_${'c'.repeat(32)}`, group_id: groupId,
      event: {
        event_id: `evt_${'c'.repeat(32)}`, run_id: newRunId, group_id: groupId,
        seq: 2, version: 2, event_type: 'status_changed',
        public_payload: { status: 'running' }, sensitivity: 'public',
        created_at: '2026-08-19T00:00:00Z', schema_version: 1,
      },
    });
    await until(
      () => useTaskRuntimeStore.getState().byRun[newRunId]?.run.last_seq === 2,
      'post-epoch runtime delta',
    );
  }, 10_000);
});

describe('W2 · 静默丢弃', () => {
  it('重复 seq 被丢弃时，走廊必须留痕（不许静默）', async () => {
    const h = connect();
    await until(() => h.statuses.includes('live'), 'live');

    const ev = server.emit('demo', { type: 'message', agent_id: 'c', content: '一' });
    await until(() => h.events.some((e) => e.type === 'message'), '第一条到');

    // 把同一条重发一遍 → seq 相同 → 必须被判 dup
    const before = getCorridorState().seqDropped;
    server['broadcast' as never] as unknown;                 // 只是提醒：下面走公开路径
    await sendRawToClients(ev);
    await until(() => getCorridorState().seqDropped > before, '★ dup 被记进走廊');

    const entry = getCorridorState().entries.at(-1)!;
    expect(entry.verdict).toBe('dup');
    expect(h.events.filter((e) => e.type === 'message')).toHaveLength(1);  // 没进 store
  });
});

describe('W3 · 心跳泄漏', () => {
  it('反复重连不会攒出多余的 ping 定时器', async () => {
    const h = connect();
    await until(() => h.statuses.includes('live'), 'live');

    for (let i = 0; i < 3; i++) {
      server.crash();
      await until(() => h.statuses.at(-1) !== 'live', `第 ${i + 1} 次断线`);
      await until(() => h.statuses.at(-1) === 'live', `第 ${i + 1} 次恢复`, 6000);
    }

    const before = server.inbound.filter((m) => m.type === 'ping').length;
    await sleep(300);
    const after = server.inbound.filter((m) => m.type === 'ping').length;

    // 心跳间隔 15s，300ms 内不该有 ping 涌进来（有 = 定时器攒了一堆）
    expect(after - before).toBeLessThanOrEqual(1);
  });
});

describe('W4 · 相互驱逐（close 4001）', () => {
  it('收到 4001 → 让位，状态 closed，且不再重连', async () => {
    const h = connect();
    await until(() => h.statuses.includes('live'), 'live');

    for (const c of (server as unknown as { clients: Set<{ ws: WS }> }).clients) {
      c.ws.close(4001, 'superseded');
    }
    await until(() => h.statuses.at(-1) === 'closed', '★ 让位为 closed', 3000);

    const statusCount = h.statuses.length;
    await sleep(800);
    expect(h.statuses.length).toBe(statusCount);   // 没有偷偷重连
  });
});

describe('W5 · seq 空洞', () => {
  it('gap → request_snapshot（带正确 project_id）', async () => {
    const h = connect();
    await until(() => h.statuses.includes('live'), 'live');

    server.emit('demo', { type: 'message', agent_id: 'c', content: '一' });
    await until(() => h.socket.watermarks.demo === 1, '水位到 1');

    server.gap('demo', 5);                                  // 凭空跳过 5 个号
    server.emit('demo', { type: 'message', agent_id: 'c', content: '二' });

    await until(
      () => server.inbound.some((m) => m.type === 'request_snapshot' && m.project_id === 'demo'),
      '★ gap 触发了带项目号的快照请求',
    );
    expect(getCorridorState().seqDropped).toBeGreaterThan(0);
    expect(getCorridorState().entries.some((e) => e.verdict === 'gap')).toBe(true);
  });

  it('恢复控制帧明确指定后台项目时只刷新该项目并回传 token', async () => {
    useTaskRuntimeStore.getState().resetAll();
    const h = connect({ projectRuntime: true });
    await until(() => h.statuses.includes('live'), 'live');
    const before = server.inbound.length;
    const groupId = `grp_${'9'.repeat(32)}`;
    const recoveryToken = 'recovery_background_once';
    const snapshotCursor = 'a'.repeat(64);
    server.emitNoSeq({
      type: 'resync_required', project_id: 'project_background', last_seq: 9,
      group_id: groupId, recovery_token: recoveryToken, recovery_generation: 1,
    });
    await until(
      () => server.inbound.slice(before).some(
        (item) => item.type === 'request_snapshot'
          && item.project_id === 'project_background'
          && item.recovery_token === recoveryToken
          && item.recovery_generation === 1,
      ),
      'background recovery snapshot request',
    );
    server.emitNoSeq({
      type: 'resync_required', project_id: 'project_background', last_seq: 9,
      group_id: groupId, recovery_token: recoveryToken, recovery_generation: 1,
    });
    await sleep(30);
    expect(server.inbound.slice(before).filter(
      (item) => item.type === 'request_snapshot' && item.recovery_token === recoveryToken,
    )).toHaveLength(1);
    expect(server.inbound.slice(before).some(
      (item) => item.type === 'request_snapshot' && item.project_id === 'demo',
    )).toBe(false);
    server.emitNoSeq({
      type: 'task_runtime_result', action: 'snapshot', request_id: recoveryToken,
      schema_version: 1, project_id: 'project_background', group_id: groupId,
      recovery_token: recoveryToken, recovery_generation: 2,
      snapshot_cursor: snapshotCursor,
      task_runtime: {
        schema_version: 1, group_id: groupId, runs: [], pending_approvals: [],
        summary: {
          active_primary_run_id: null, primary_task_status: null, queued_task_count: 0,
          active_side_task_count: 0, pending_approval_count: 0,
          budget_usage: {
            wall_clock_seconds: 0, model_turns: 0, tool_calls: 0, tokens: 0, cost_usd: 0,
          },
          indexing_state: 'idle',
        },
      },
    });
    await until(() => h.runtimeSnapshots.length === 1, 'background Runtime snapshot handoff');
    expect(h.runtimeSnapshots[0]?.groupId).toBe(groupId);
    expect(useTaskRuntimeStore.getState().byGroup[groupId as GroupId]).toEqual([]);
    await until(() => server.inbound.some(
      (item) => item.type === 'task_runtime_recovery_ack'
        && item.project_id === 'project_background'
        && item.recovery_token === recoveryToken
        && item.recovery_generation === 2
        && item.snapshot_cursor === snapshotCursor,
    ), 'renderer ack');
    expect(h.statuses.at(-1)).toBe('live');
  });

  it('多个项目并行恢复时，最后一个完成前全局状态保持 resync', async () => {
    const h = connect({ projectRuntime: true });
    await until(() => h.statuses.includes('live'), 'live');
    const groups = [`grp_${'6'.repeat(32)}`, `grp_${'7'.repeat(32)}`] as const;
    const projects = ['project_recovery_a', 'project_recovery_b'] as const;
    const tokens = ['recovery_multi_a', 'recovery_multi_b'] as const;
    for (let index = 0; index < projects.length; index += 1) {
      server.emitNoSeq({
        type: 'resync_required', project_id: projects[index], last_seq: index + 1,
        group_id: groups[index], recovery_token: tokens[index], recovery_generation: 1,
      });
    }
    await until(() => tokens.every((token) => server.inbound.some(
      (item) => item.type === 'request_snapshot' && item.recovery_token === token,
    )), 'both recovery requests');

    const respond = (index: number): void => {
      server.emitNoSeq({
        type: 'task_runtime_result', action: 'snapshot', request_id: tokens[index],
        schema_version: 1, project_id: projects[index], group_id: groups[index],
        recovery_token: tokens[index], recovery_generation: 1,
        snapshot_cursor: String(index + 1).repeat(64),
        task_runtime: {
          schema_version: 1, group_id: groups[index], runs: [], pending_approvals: [],
          summary: {
            active_primary_run_id: null, primary_task_status: null, queued_task_count: 0,
            active_side_task_count: 0, pending_approval_count: 0,
            budget_usage: {
              wall_clock_seconds: 0, model_turns: 0, tool_calls: 0, tokens: 0, cost_usd: 0,
            },
            indexing_state: 'idle',
          },
        },
      });
    };

    respond(0);
    await until(() => server.inbound.some(
      (item) => item.type === 'task_runtime_recovery_ack'
        && item.recovery_token === tokens[0],
    ), 'first recovery ack');
    expect(h.statuses.at(-1)).toBe('resync');
    respond(1);
    await until(() => server.inbound.some(
      (item) => item.type === 'task_runtime_recovery_ack'
        && item.recovery_token === tokens[1],
    ), 'second recovery ack');
    expect(h.statuses.at(-1)).toBe('live');
  });

  it('同一 durable token 断线重连后可重试；畸形快照不 ACK 且保持 resync', async () => {
    const h = connect();
    await until(() => h.statuses.includes('live'), 'live');
    const groupId = `grp_${'8'.repeat(32)}`;
    const notice = {
      type: 'resync_required', project_id: 'project_retry', last_seq: 3,
      group_id: groupId, recovery_token: 'recovery_retry', recovery_generation: 4,
    } as const;
    server.emitNoSeq(notice);
    await until(() => server.inbound.filter(
      (item) => item.type === 'request_snapshot' && item.recovery_token === 'recovery_retry',
    ).length === 1, 'first recovery request');

    server.restart();
    await until(() => h.statuses.at(-1) === 'live', 'reconnected live', 6000);
    server.emitNoSeq(notice);
    await until(() => server.inbound.filter(
      (item) => item.type === 'request_snapshot' && item.recovery_token === 'recovery_retry',
    ).length === 2, 'same token retried', 6000);
    server.emitNoSeq({
      type: 'task_runtime_result', action: 'snapshot', request_id: 'recovery_retry',
      schema_version: 1, project_id: 'project_retry', group_id: groupId,
      recovery_token: 'recovery_retry', recovery_generation: 4,
      snapshot_cursor: 'not-a-cursor', task_runtime: { group_id: groupId },
    });
    await sleep(50);
    expect(server.inbound.some((item) => item.type === 'task_runtime_recovery_ack')).toBe(false);
    expect(h.statuses.at(-1)).toBe('resync');
  });
});

describe('W6 · 字段漂移', () => {
  it('畸形事件 → Zod 拒收 → 走廊计数 +1，绝不进 store', async () => {
    const h = connect();
    await until(() => h.statuses.includes('live'), 'live');

    // stream_delta 用了 text 而不是 content（真实发生过的漂移）
    await sendRawToClients({
      type: 'stream_delta', agent_id: 'c', text: '你好',
      project_id: 'demo', seq: 1, ts: new Date().toISOString(),
    });

    await until(() => getCorridorState().zodRejected > 0, '★ Zod 拒收被记入走廊');
    expect(h.events.some((e) => e.type === 'stream_delta')).toBe(false);
  });
});

describe('W7 · 广播失聪', () => {
  it('5s 没等到 user_echo → 哨兵告警（这是失聪唯一的可观察信号）', async () => {
    const h = connect();
    await until(() => h.statuses.includes('live'), 'live');

    server.muted = true;                       // 服务端收得到，但一条都不广播
    h.socket.sendMessage('喂', 'demo', 'cm_deaf');

    await until(() => h.echoLost.includes('cm_deaf'), '★ 哨兵告警', 8000);
    expect(getCorridorState().sentinelAlerts).toBeGreaterThan(0);
  }, 12000);
});

describe('W8 · 握手乱序', () => {
  it('实时事件先于回放到达 → 进缓冲，历史零丢失', async () => {
    // 先造 3 条历史
    server.say('demo', 'coordinator', 'ab', 1);   // thinking + 2 deltas + message

    const h = connect();
    await until(() => h.statuses.includes('handshaking'), '进入握手');

    // 握手窗口里插一条实时高 seq 事件（回放还没回来）
    server.emit('demo', { type: 'message', agent_id: 'c', content: '插队的实时消息' });

    await until(() => h.statuses.includes('live'), 'live', 5000);
    await sleep(120);

    // 历史 message + 插队 message 都在，且 seq 单调
    const seqs = h.events
      .map((e) => (e as unknown as { seq?: number }).seq)
      .filter((s): s is number => typeof s === 'number');
    expect(seqs).toEqual([...seqs].sort((a, b) => a - b));   // ★ 排过序
    expect(new Set(seqs).size).toBe(seqs.length);            // ★ 去过重
    expect(h.events.filter((e) => e.type === 'message')).toHaveLength(2); // ★ 一条都没丢
    expect(getCorridorState().entries.some((e) => e.verdict === 'buffered')).toBe(true);
  });
});

describe('W9 · 跨项目污染', () => {
  it('B 的事件不影响 A 的水位（每个项目一条水位线）', async () => {
    const h = connect();
    await until(() => h.statuses.includes('live'), 'live');

    server.emit('demo', { type: 'message', agent_id: 'c', content: 'A1' });
    await until(() => h.socket.watermarks.demo === 1, 'A 水位 1');

    // B 项目连发 5 条（seq 1..5）——A 的水位不该动，也不该被判 gap
    for (let i = 0; i < 5; i++) {
      server.emit('other', { type: 'message', agent_id: 'c', content: `B${i}` });
    }
    await until(() => h.socket.watermarks.other === 5, 'B 水位 5');

    expect(h.socket.watermarks.demo).toBe(1);                 // ★ A 纹丝不动
    expect(h.events.filter((e) => e.type === 'message')).toHaveLength(6);
  });
});

describe('W10 · 双解决', () => {
  it('timeout + cancelled 先后到达，两条都过 Zod（前端首个解决为准）', async () => {
    const h = connect();
    await until(() => h.statuses.includes('live'), 'live');

    const cardId = server.proposeAgents('demo');
    await until(() => h.events.some((e) => e.type === 'approval_card'), '卡到了');

    server.doubleResolve('demo', cardId, 'timeout', 'cancelled');
    await until(
      () => h.events.filter((e) => e.type === 'approval_resolved').length === 2,
      '★ 两条 resolution 都过了 Zod（没被拒收）',
    );

    const resolutions = h.events
      .filter((e) => e.type === 'approval_resolved')
      .map((e) => (e as unknown as { resolution: string }).resolution);
    expect(resolutions).toEqual(['timeout', 'cancelled']);
    // 幂等由 state.ts 负责（见 state.test.ts 的「首个解决为准」）
  });
});

describe('W11 · 出站失败', () => {
  it('未连接时发消息 → 响亮失败（走廊计数 +1），绝不静默吞掉', async () => {
    const h = connect();
    await until(() => h.statuses.includes('live'), 'live');

    h.socket.disconnect();
    await sleep(50);

    const before = getCorridorState().outboundFailed;
    h.socket.sendMessage('断线也要发', 'demo', 'cm_offline');

    expect(getCorridorState().outboundFailed).toBe(before + 1);   // ★ 出声了
    const entry = getCorridorState().entries.at(-1)!;
    expect(entry.verdict).toBe('failed');
  });
});

describe('W12 · 无 seq 白名单旁路', () => {
  it('pong / project_created 不参与水位（否则水位会被凭空拉高）', async () => {
    const h = connect();
    await until(() => h.statuses.includes('live'), 'live');

    server.emit('demo', { type: 'message', agent_id: 'c', content: '一' });
    await until(() => h.socket.watermarks.demo === 1, '水位 1');

    await sendRawToClients({ type: 'pong' });
    await sendRawToClients({ type: 'project_created', project_id: 'demo', project_name: 'X' });
    await sleep(80);

    expect(h.socket.watermarks.demo).toBe(1);                    // ★ 水位没被污染
    expect(getCorridorState().seqDropped).toBe(0);               // 也没被误判成丢弃
  });
});

describe('W13 · 权威活动账本转交', () => {
  it('replay_complete 带 activity → onActivityLedger 收到（项目 + 全量条目）', async () => {
    const activity = [
      { agent_id: 'pm_1', scope_id: '', channel_id: 'demo', started_at: 1720000000000 },
      { agent_id: 'fe_1', scope_id: 's_1', channel_id: 'demo', started_at: 1720000001000 },
    ];
    const h = connect();
    // 无配置的 FakeServer 不带 activity → 先确认连接正常（不触发）
    await until(() => h.statuses.includes('live'), 'live');
    expect(h.ledger).toHaveLength(0);

    // 断开重连：这次注入账本（FakeServer 配置在 beforeEach 里创建，需换实例）
    h.socket.disconnect();
    await sleep(50);
    const port = Number(new URL(server.url).port);
    await server.stop();
    server = new FakeKnoweServer({ port, ringMax: 5, replayActivity: activity });
    await server.start();
    h.socket.connect();
    await until(() => h.ledger.length > 0, '账本转交');
    expect(h.ledger[0]!.pid).toBe('demo');
    expect(h.ledger[0]!.activity).toEqual(activity);
  });

  it('replay_complete 不带 activity（旧后端）→ 不触发回调', async () => {
    const h = connect();
    await until(() => h.statuses.includes('live'), 'live');
    await sleep(80);
    expect(h.ledger).toHaveLength(0);
  });
});

// ═══════════════════════════════════════════════════════════════
// 工具：绕过 FakeServer 的 emit，直接把原始帧推给所有客户端
// （用来注入畸形事件 / 重复事件 —— 正常路径发不出这种东西）
// ═══════════════════════════════════════════════════════════════
async function sendRawToClients(raw: Record<string, unknown>): Promise<void> {
  const inner = server as unknown as { clients: Set<{ ws: WS }> };
  const text = JSON.stringify(raw);
  for (const c of inner.clients) {
    if (c.ws.readyState === WS.OPEN) c.ws.send(text);
  }
  await sleep(30);
}
