/**
 * 后端公开事件契约与前端镜像的防漂移测试。
 *
 * `RunEventSchema` 用的是 `parse`，所以它是一道**逐字段闸门**：后端广播的合法事件只要
 * 有一个字段（或整个事件类型）没在前端登记，事件就被拒绝，而 `App.tsx` 的
 * `applyEvent` 和 `socket.ts` 的 `onEvent` 都没有 try/catch —— 那一批事件的投递就地
 * 中断。这不是理论风险：在补上这份测试之前，前端缺 6 种事件类型（含每次工具调用都会
 * 产生的 `tool_completed`）和 `budget_updated.settled`，实测全部被拒。
 *
 * ⚠ 这份测试的诚实边界：它**读后端的源码文本**，不是运行时导入（Python 定义不可能被
 * vitest import）。所以它挡不住"把字典改写成另一种语法导致正则失配"——为了不把那种
 * 情况变成静默通过，解析一旦失配就直接断言失败（fail closed），而不是跳过。
 */

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import { RunEventSchema, publicEventFields } from './taskRuntime';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CONTRACTS = path.resolve(HERE, '..', '..', 'backend', 'task_runtime', 'contracts.py');

function backendPublicEventFields(): Record<string, Set<string>> {
  const source = readFileSync(CONTRACTS, 'utf8');
  // 行首精确匹配：`^` 配 `m` 标志，所以 `_RENAMED_PUBLIC_EVENT_FIELDS = {...}` 这样的
  // 重命名**不会**被当成命中（子串正则会，于是改名后守卫还能"通过"，那是假的安心）。
  // 字典的结束是行首的 `}`（内部每行都以 `    }),` 结尾，不会误匹配）。
  const block = /^_PUBLIC_EVENT_FIELDS[^=]*=\s*\{(.*?)\n\}/ms.exec(source);
  if (!block) {
    throw new Error(
      `could not parse _PUBLIC_EVENT_FIELDS out of ${CONTRACTS}; `
      + 'the drift guard cannot run, and a guard that silently passes is worse than none',
    );
  }
  const parsed: Record<string, Set<string>> = {};
  const body = block[1] ?? '';
  for (const match of body.matchAll(/"(\w+)":\s*frozenset\(\{([^}]*)\}\)/g)) {
    const name = match[1] ?? '';
    const fields = match[2] ?? '';
    parsed[name] = new Set([...fields.matchAll(/"([^"]*)"/g)].map((m) => m[1] ?? ''));
  }
  if (Object.keys(parsed).length === 0) {
    throw new Error(`parsed zero event types out of ${CONTRACTS}; the guard is not running`);
  }
  return parsed;
}

const BASE_EVENT = {
  event_id: `evt_${'6'.repeat(32)}`,
  run_id: `run_${'7'.repeat(32)}`,
  group_id: `grp_${'8'.repeat(32)}`,
  seq: 2,
  version: 2,
  sensitivity: 'public',
  created_at: '2026-09-21T00:00:00Z',
  schema_version: 1,
};

const USAGE = {
  tokens: 10, model_turns: 1, tool_calls: 1, wall_clock_seconds: 0.5, cost_usd: 0.01,
};

describe('public event contract drift', () => {
  it('mirrors every backend event type and payload field', () => {
    const backend = backendPublicEventFields();
    const backendNames = Object.keys(backend).sort();
    const frontendNames = Object.keys(publicEventFields).sort();

    expect(frontendNames).toEqual(backendNames);
    for (const name of backendNames) {
      const frontend = publicEventFields[name];
      if (!frontend) throw new Error(`frontend has no whitelist for ${name}`);
      expect([...frontend].sort()).toEqual([...(backend[name] ?? new Set<string>())].sort());
    }
  });

  it('accepts the events the backend actually broadcasts', () => {
    // 这几种在过去都被前端拒过。断言的是"要求"（后端合法事件必须被接受），
    // 不是"观察到的行为"。
    const samples: Array<[string, Record<string, unknown>]> = [
      ['provider_egress/authorized', {
        status: 'authorized', capability_digest: 'a'.repeat(64), privacy: 'standard',
        usage: USAGE, approval_required: false,
      }],
      ['provider_egress/terminal', {
        status: 'completed', capability_digest: 'a'.repeat(64), privacy: 'standard',
        duration_ms: 12, approval_required: false, actual_tokens: 15,
      }],
      ['tool_claimed', {
        status: 'running', invocation_id: `tool_${'1'.repeat(32)}`, tool_name: 'web_search',
        purpose: 'read', risk: 'low', started_at: '2026-09-21T00:00:00Z', usage: USAGE,
      }],
      ['tool_completed', {
        status: 'completed', invocation_id: `tool_${'1'.repeat(32)}`, tool_name: 'web_search',
        purpose: 'read', risk: 'low', started_at: '2026-09-21T00:00:00Z',
        completed_at: '2026-09-21T00:00:01Z', duration_ms: 1000, usage: USAGE, error_type: '',
      }],
      ['tool_cancelled', {
        status: 'cancelled', invocation_id: `tool_${'1'.repeat(32)}`,
        tool_name: 'provider_egress', purpose: 'provider_egress', risk: 'high',
        started_at: '2026-09-21T00:00:00Z', completed_at: '2026-09-21T00:00:01Z',
        duration_ms: 1000, usage: USAGE, error_type: '',
      }],
      ['tool_recovered_blocked', {
        status: 'recovered_blocked', invocation_id: `tool_${'1'.repeat(32)}`,
        tool_name: 'external_send', purpose: 'send_result', risk: 'high',
        started_at: '2026-09-21T00:00:00Z', completed_at: '2026-09-21T00:00:01Z',
        duration_ms: 1000, usage: USAGE, error_type: 'RestartRecoveryBlocked',
      }],
      ['budget_updated/settled', {
        status: 'running', usage: USAGE, settled: true,
      }],
    ];

    const rejected: string[] = [];
    for (const [label, public_payload] of samples) {
      const result = RunEventSchema.safeParse({
        ...BASE_EVENT,
        event_type: label.split('/')[0],
        public_payload,
      });
      if (!result.success) {
        rejected.push(`${label}: ${result.error.issues.map((i) => i.message).join('; ')}`);
      }
    }
    expect(rejected).toEqual([]);
  });
});
