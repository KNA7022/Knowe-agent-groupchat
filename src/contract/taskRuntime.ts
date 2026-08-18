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
  'budget_exhausted', 'verification_failed', 'irreversible_side_effect',
]);

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
}).strict();

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
});

const PublicPayloadSchema = z.record(z.unknown()).superRefine((payload, context) => {
  const privateKeys = new Set([
    'chain_of_thought', 'reasoning_content', 'raw_reasoning', 'system_prompt',
    'api_key', 'secret', 'credential', 'private_payload', 'sensitive_payload',
  ]);
  const walk = (value: unknown): boolean => {
    if (Array.isArray(value)) return value.some(walk);
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
}).strict();

export const ApprovalRequestSchema = z.object({
  approval_id: z.string().regex(/^apr_[0-9a-f]{32}$/),
  run_id: z.string().regex(/^run_[0-9a-f]{32}$/),
  group_id: GroupIdSchema,
  kind: z.string().min(1),
  summary: z.string(),
  risk: RiskLevelSchema,
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
}).strict();

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
