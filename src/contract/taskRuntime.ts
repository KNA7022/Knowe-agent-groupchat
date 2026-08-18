import { z } from 'zod';

import { GroupIdSchema } from './group';

export const TaskRuntimeSchemaVersionSchema = z.literal(1);
export const TaskStatusSchema = z.enum([
  'queued', 'routing', 'running', 'waiting_approval', 'paused', 'verifying',
  'succeeded', 'partial', 'failed', 'cancelled',
]);
export const AgentPhaseSchema = z.enum([
  'idle', 'planning', 'retrieving', 'executing', 'waiting', 'verifying',
  'responding', 'failed',
]);
export const RunKindSchema = z.enum(['primary', 'side']);
export const QueueClassSchema = z.enum(['foreground', 'index']);
export const RouteLabelSchema = z.enum(['supplement_current', 'side_answer', 'queue_new']);
export const RiskLevelSchema = z.enum(['low', 'medium', 'high']);
export const FailureTypeSchema = z.enum([
  'parameter_error', 'transient_provider_failure', 'permission_denied',
  'budget_exhausted', 'verification_failed', 'executor_failure', 'irreversible_side_effect',
]);
const PublicClassificationSchema = z.enum([
  'credential', 'contact_pii', 'identity_pii', 'student_identifier',
  'academic_record', 'name_pii', 'timetable_association', 'private_note',
]);

const publicValueIsSensitive = (value: unknown): boolean => {
  if (Array.isArray(value)) return value.some(publicValueIsSensitive);
  if (typeof value === 'string') {
    if (/^(?:(?:run|task|evt|arun|apr|grant|tool|ckp|vfd|grp)_[0-9a-f]{32}|[0-9a-f]{64})$/.test(value)) {
      return false;
    }
    return /(?:authorization\s*:\s*bearer\s+\S+|\bbearer\s+(?:sk-|pk-|tok_)?[A-Za-z0-9._-]{6,}|\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|cookie)\s*[:=]\s*\S+|\bsk-[A-Za-z0-9._-]{6,})/i.test(value)
      || /\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/i.test(value)
      || /(^|\D)1[3-9]\d{9}(\D|$)/.test(value)
      || /(^|\D)\d{17}[0-9Xx](\D|$)/.test(value)
      || /(?:学号|student\s*id)\s*[:：#]?\s*[A-Za-z0-9_-]{4,}/i.test(value)
      || /(?:成绩|分数|绩点|排名|名次|grade|score|class\s+rank|academic\s+rank)\s*(?:为|是|[:：]?\s*)?(?:第\s*)?\d+(?:\.\d+)?(?:\s*[分名])?/i.test(value)
      || /[\u4e00-\u9fff]{2,4}(?=.{0,10}(?:成绩|分数|绩点|排名|名次))/.test(value)
      || /(?:[\u4e00-\u9fff]{2,4}的(?:课表|课程表|上课时间)|姓名\s*[:：]\s*[\u4e00-\u9fff]{2,4}.{0,12}(?:课表|课程表|上课时间))/.test(value)
      || /(?:私人|私密|个人)笔记|private\s+(?:note|journal)/i.test(value);
  }
  if (!value || typeof value !== 'object') return false;
  return Object.values(value as Record<string, unknown>).some(publicValueIsSensitive);
};

export const BudgetLimitsSchema = z.object({
  wall_clock_seconds: z.number().int().nonnegative(),
  model_turns: z.number().int().nonnegative(),
  tool_calls: z.number().int().nonnegative(),
  tokens: z.number().int().nonnegative(),
  cost_usd: z.number().nonnegative(),
}).strict();

export const BudgetUsageSchema = z.object({
  wall_clock_seconds: z.number().nonnegative(),
  model_turns: z.number().int().nonnegative(),
  tool_calls: z.number().int().nonnegative(),
  tokens: z.number().int().nonnegative(),
  cost_usd: z.number().nonnegative(),
}).strict();

export const EvidenceRefSchema = z.object({
  evidence_id: z.string().min(1),
  source_id: z.string().min(1),
  version_id: z.string().min(1),
  locator: z.string().min(1),
  excerpt_ref: z.string(),
  schema_version: TaskRuntimeSchemaVersionSchema,
}).strict();

export const AgentRunSchema = z.object({
  agent_run_id: z.string().regex(/^arun_[0-9a-f]{32}$/),
  run_id: z.string().regex(/^run_[0-9a-f]{32}$/),
  agent_id: z.string().min(1),
  role: z.string().min(1),
  phase: AgentPhaseSchema,
  operation_summary: z.string(),
  blocking_reason: z.string(),
  version: z.number().int().positive(),
  updated_at: z.string().min(1),
  schema_version: TaskRuntimeSchemaVersionSchema,
}).strict().superRefine((agent, context) => {
  if (publicValueIsSensitive([agent.operation_summary, agent.blocking_reason])) {
    context.addIssue({ code: z.ZodIssueCode.custom, message: 'sensitive Agent status text' });
  }
});

export const TaskRunSchema = z.object({
  run_id: z.string().regex(/^run_[0-9a-f]{32}$/),
  task_id: z.string().regex(/^task_[0-9a-f]{32}$/),
  group_id: GroupIdSchema,
  kind: RunKindSchema,
  title: z.string().min(1),
  status: TaskStatusSchema,
  queue_class: QueueClassSchema,
  route_label: RouteLabelSchema,
  read_only: z.boolean(),
  risk: RiskLevelSchema,
  parent_run_id: z.string().regex(/^run_[0-9a-f]{32}$/).nullable(),
  version: z.number().int().positive(),
  last_seq: z.number().int().nonnegative(),
  budget_limits: BudgetLimitsSchema,
  budget_usage: BudgetUsageSchema,
  agent_runs: z.array(AgentRunSchema),
  evidence: z.array(EvidenceRefSchema),
  failure_type: FailureTypeSchema.nullable(),
  public_summary: z.string(),
  blocking_reason: z.string(),
  data_classification: z.array(PublicClassificationSchema).default([]),
  created_at: z.string().min(1),
  updated_at: z.string().min(1),
  started_at: z.string().nullable(),
  finished_at: z.string().nullable(),
  schema_version: TaskRuntimeSchemaVersionSchema,
}).strict().superRefine((run, context) => {
  if (run.kind === 'side' && (!run.read_only || run.risk !== 'low')) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      message: 'side runs must be low-risk and read-only',
    });
  }
  if (run.queue_class === 'index' && run.kind !== 'side') {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      message: 'index jobs must use isolated side-run capacity',
    });
  }
  if (publicValueIsSensitive([run.title, run.public_summary, run.blocking_reason])) {
    context.addIssue({ code: z.ZodIssueCode.custom, message: 'sensitive TaskRun public text' });
  }
});

const publicEventFields: Record<string, ReadonlySet<string>> = {
  run_queued: new Set(['status', 'kind', 'route_label', 'summary', 'classification']),
  status_changed: new Set(['from', 'status', 'summary', 'blocking_reason', 'failure_type']),
  agent_phase_changed: new Set(['agent_id', 'role', 'phase', 'operation_summary', 'blocking_reason']),
  budget_updated: new Set(['usage', 'limits', 'exhausted_dimension', 'status']),
  budget_exhausted: new Set(['usage', 'limits', 'exhausted_dimension', 'status']),
  approval_requested: new Set(['status', 'approval']),
  approval_resolved: new Set(['status', 'approval_id', 'resolution', 'failure_type']),
  checkpoint_created: new Set(['label']),
  message_routed: new Set(['route_label', 'summary', 'classification']),
  restart_recovery: new Set(['status', 'blocking_reason']),
  tool_prepared: new Set(['status', 'invocation_id', 'tool_name', 'disposition', 'usage']),
  verifier_decision: new Set(['status', 'decision_id', 'outcome', 'approved', 'evidence_count']),
};

const PublicPayloadSchema = z.record(z.unknown()).superRefine((payload, context) => {
  const privateKeys = new Set([
    'chain_of_thought', 'reasoning_content', 'raw_reasoning', 'system_prompt',
    'api_key', 'secret', 'credential', 'private_payload', 'sensitive_payload',
    'token', 'access_token', 'refresh_token', 'password', 'authorization', 'cookie',
    'student_id', 'phone', 'email', 'id_card', 'identity_number', 'grade', 'rank',
  ]);
  const walk = (value: unknown): boolean => {
    if (Array.isArray(value)) return value.some(walk);
    if (typeof value === 'string') return publicValueIsSensitive(value);
    if (!value || typeof value !== 'object') return false;
    return Object.entries(value).some(([key, child]) => privateKeys.has(key.toLowerCase()) || walk(child));
  };
  if (walk(payload)) {
    context.addIssue({ code: z.ZodIssueCode.custom, message: 'private data in public event' });
  }
});

export const RunEventSchema = z.object({
  event_id: z.string().regex(/^evt_[0-9a-f]{32}$/),
  run_id: z.string().regex(/^run_[0-9a-f]{32}$/),
  group_id: GroupIdSchema,
  seq: z.number().int().positive(),
  version: z.number().int().positive(),
  event_type: z.string().min(1),
  public_payload: PublicPayloadSchema,
  sensitivity: z.string().min(1),
  created_at: z.string().min(1),
  schema_version: TaskRuntimeSchemaVersionSchema,
}).strict().superRefine((event, context) => {
  const fields = publicEventFields[event.event_type];
  if (!fields) {
    context.addIssue({ code: z.ZodIssueCode.custom, message: 'unsupported public event type' });
    return;
  }
  const extra = Object.keys(event.public_payload).filter((field) => !fields.has(field));
  if (extra.length) {
    context.addIssue({ code: z.ZodIssueCode.custom, message: `unsupported public fields: ${extra.join(',')}` });
  }
  if ('classification' in event.public_payload
      && !z.array(PublicClassificationSchema).safeParse(event.public_payload.classification).success) {
    context.addIssue({ code: z.ZodIssueCode.custom, message: 'invalid public classification metadata' });
  }
});

export const ApprovalRequestSchema = z.object({
  approval_id: z.string().regex(/^apr_[0-9a-f]{32}$/),
  run_id: z.string().regex(/^run_[0-9a-f]{32}$/),
  group_id: GroupIdSchema,
  kind: z.string().min(1),
  summary: z.string(),
  risk: RiskLevelSchema,
  requested_capabilities: z.array(z.string()),
  tool_name: z.string(),
  operation_scope: z.record(z.string()),
  purpose: z.string().min(1),
  expires_at: z.string().nullable(),
  status: z.enum(['pending', 'approved', 'rejected']),
  created_at: z.string().min(1),
  resolved_at: z.string().nullable(),
  schema_version: TaskRuntimeSchemaVersionSchema,
}).strict();

export const PermissionGrantSchema = z.object({
  grant_id: z.string().regex(/^grant_[0-9a-f]{32}$/),
  approval_id: z.string().regex(/^apr_[0-9a-f]{32}$/),
  run_id: z.string().regex(/^run_[0-9a-f]{32}$/),
  group_id: GroupIdSchema,
  task_id: z.string().regex(/^task_[0-9a-f]{32}$/),
  capabilities: z.array(z.string()),
  tool_name: z.string(),
  operation_scope: z.record(z.string()),
  purpose: z.string().min(1),
  expires_at: z.string().nullable(),
  created_at: z.string().min(1),
  schema_version: TaskRuntimeSchemaVersionSchema,
}).strict();

export const ToolInvocationSchema = z.object({
  invocation_id: z.string().regex(/^tool_[0-9a-f]{32}$/),
  run_id: z.string().regex(/^run_[0-9a-f]{32}$/),
  group_id: GroupIdSchema,
  tool_name: z.string().min(1),
  idempotency_key: z.string().min(1),
  risk: RiskLevelSchema,
  status: z.string().min(1),
  replay_allowed: z.boolean(),
  public_summary: z.string(),
  operation_scope: z.record(z.string()),
  purpose: z.string().min(1),
  disposition: z.enum(['created', 'requires_new_approval', 'recovered_blocked']),
  created_at: z.string().min(1),
  completed_at: z.string().nullable(),
  schema_version: TaskRuntimeSchemaVersionSchema,
}).strict().refine((tool) => tool.risk !== 'high' || !tool.replay_allowed, {
  message: 'high-risk tools cannot be replayable',
});

export const CheckpointSchema = z.object({
  checkpoint_id: z.string().regex(/^ckp_[0-9a-f]{32}$/),
  run_id: z.string().regex(/^run_[0-9a-f]{32}$/),
  group_id: GroupIdSchema,
  seq: z.number().int().positive(),
  version: z.number().int().positive(),
  label: z.string().min(1),
  resumable: z.boolean(),
  created_at: z.string().min(1),
  schema_version: TaskRuntimeSchemaVersionSchema,
}).strict();

export const RuntimeReplaySchema = z.object({
  schema_version: TaskRuntimeSchemaVersionSchema,
  run_id: z.string().regex(/^run_[0-9a-f]{32}$/),
  group_id: GroupIdSchema,
  after_seq: z.number().int().nonnegative(),
  last_seq: z.number().int().nonnegative(),
  version: z.number().int().positive(),
  gap: z.boolean(),
  events: z.array(RunEventSchema),
  snapshot: z.object({
    schema_version: TaskRuntimeSchemaVersionSchema,
    run: TaskRunSchema,
    last_seq: z.number().int().nonnegative(),
    version: z.number().int().positive(),
  }).strict().nullable(),
}).strict().superRefine((replay, context) => {
  const issue = (message: string) => context.addIssue({ code: z.ZodIssueCode.custom, message });
  if (replay.snapshot) {
    if (replay.snapshot.run.run_id !== replay.run_id
        || replay.snapshot.run.group_id !== replay.group_id) {
      issue('replay snapshot crosses the addressed identity boundary');
    }
    if (replay.snapshot.last_seq !== replay.snapshot.run.last_seq
        || replay.snapshot.version !== replay.snapshot.run.version
        || replay.snapshot.last_seq !== replay.last_seq
        || replay.snapshot.version !== replay.version) {
      issue('replay snapshot cursor does not match its nested run');
    }
  }
  if (replay.gap && !replay.snapshot) issue('replay gap requires an authoritative snapshot');
  if (!replay.gap && replay.snapshot) issue('contiguous replay must not replace with a snapshot');
  replay.events.forEach((event, index) => {
    if (event.run_id !== replay.run_id || event.group_id !== replay.group_id) {
      issue('replay event crosses the addressed identity boundary');
    }
    if (event.seq !== replay.after_seq + index + 1) issue('replay event sequence is not contiguous');
  });
  if (!replay.gap) {
    const last = replay.events[replay.events.length - 1];
    if (last && (last.seq !== replay.last_seq || last.version !== replay.version)) {
      issue('replay event tail does not match the replay cursor');
    }
    if (!last && replay.after_seq !== replay.last_seq) {
      issue('empty replay does not match the requested cursor');
    }
  }
});

export const GroupRuntimeSnapshotSchema = z.object({
  schema_version: TaskRuntimeSchemaVersionSchema,
  group_id: GroupIdSchema,
  runs: z.array(TaskRunSchema),
  pending_approvals: z.array(ApprovalRequestSchema),
  summary: z.object({
    active_primary_run_id: z.string().regex(/^run_[0-9a-f]{32}$/).nullable(),
    primary_task_status: TaskStatusSchema.nullable(),
    queued_task_count: z.number().int().nonnegative(),
    active_side_task_count: z.number().int().nonnegative(),
    pending_approval_count: z.number().int().nonnegative(),
    budget_usage: BudgetUsageSchema,
    indexing_state: z.enum(['idle', 'queued', 'running', 'failed']),
  }).strict(),
}).strict();

export const GroupRuntimeSnapshotResponseSchema = z.object({
  ok: z.literal(true),
  schema_version: TaskRuntimeSchemaVersionSchema,
  group_id: GroupIdSchema,
  runs: z.array(TaskRunSchema),
  pending_approvals: z.array(ApprovalRequestSchema),
  summary: GroupRuntimeSnapshotSchema.shape.summary,
}).strict();

export type TaskStatus = z.infer<typeof TaskStatusSchema>;
export type AgentPhase = z.infer<typeof AgentPhaseSchema>;
export type RunKind = z.infer<typeof RunKindSchema>;
export type QueueClass = z.infer<typeof QueueClassSchema>;
export type RouteLabel = z.infer<typeof RouteLabelSchema>;
export type RiskLevel = z.infer<typeof RiskLevelSchema>;
export type BudgetLimits = z.infer<typeof BudgetLimitsSchema>;
export type BudgetUsage = z.infer<typeof BudgetUsageSchema>;
export type EvidenceRef = z.infer<typeof EvidenceRefSchema>;
export type AgentRun = z.infer<typeof AgentRunSchema>;
export type TaskRun = z.infer<typeof TaskRunSchema>;
export type RunEvent = z.infer<typeof RunEventSchema>;
export type ApprovalRequest = z.infer<typeof ApprovalRequestSchema>;
export type PermissionGrant = z.infer<typeof PermissionGrantSchema>;
export type ToolInvocation = z.infer<typeof ToolInvocationSchema>;
export type Checkpoint = z.infer<typeof CheckpointSchema>;
export type RuntimeReplay = z.infer<typeof RuntimeReplaySchema>;
export type GroupRuntimeSnapshot = z.infer<typeof GroupRuntimeSnapshotSchema>;
