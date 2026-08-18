import { z } from 'zod';

/** Canonical StudyCrew Group contract. Legacy project_id exists only in transport aliases. */
export const GroupIdSchema = z.string().regex(/^grp_[0-9a-f]{32}$/);
export const GroupAgentIdSchema = z.string().regex(/^gag_[0-9a-f]{32}$/);
export const GroupCourseBindingIdSchema = z.string().regex(/^gcb_[0-9a-f]{32}$/);
export const GroupSourceIdSchema = z.string().regex(/^src_[0-9a-f]{32}$/);
export const GroupSourceLinkIdSchema = z.string().regex(/^gsl_[0-9a-f]{32}$/);

export const GroupTemplateSchema = z.enum([
  'course_study', 'exam_sprint', 'general_assistant', 'custom',
]);
export const GroupLifecycleSchema = z.enum(['active', 'archived']);
export const GroupAgentRoleSchema = z.enum([
  'coordinator', 'verifier', 'knowledge', 'planning', 'coach', 'expert', 'legacy_expert',
]);

export const ExpertTemplateConfigSchema = z.object({
  name: z.string().trim().min(1).max(80),
  specialty: z.string().trim().min(1).max(200),
  responsibilities: z.array(z.string().trim().min(1).max(240)).max(12),
  allowed_tools: z.array(z.string().trim().min(1).max(80)).max(32),
  model_override: z.string().trim().min(1).max(160).nullable().optional(),
  budget: z.number().int().nonnegative().nullable().optional(),
  /** This is asserted by the backend and cannot be changed by expert input. */
  platform_policy_locked: z.literal(true),
}).strict();

export const GroupAgentSchema = z.object({
  agent_id: GroupAgentIdSchema,
  group_id: GroupIdSchema,
  role: GroupAgentRoleSchema,
  display_name: z.string().min(1),
  template_key: z.string().nullable().optional(),
  expert: ExpertTemplateConfigSchema.nullable().optional(),
  model_override: z.string().nullable().optional(),
  tool_overrides: z.array(z.string()),
  budget: z.number().int().nonnegative().nullable().optional(),
  created_at: z.string(),
  updated_at: z.string(),
});

export const GroupCourseBindingSchema = z.object({
  binding_id: GroupCourseBindingIdSchema,
  group_id: GroupIdSchema,
  course_id: z.string().min(1),
  created_at: z.string(),
});

export const GroupSourceLinkSchema = z.object({
  link_id: GroupSourceLinkIdSchema,
  group_id: GroupIdSchema,
  source_id: GroupSourceIdSchema,
  created_at: z.string(),
});

export const GroupSchema = z.object({
  schema_version: z.literal(1),
  group_id: GroupIdSchema,
  name: z.string().trim().min(1).max(160),
  template: GroupTemplateSchema,
  lifecycle: GroupLifecycleSchema,
  roster: z.array(GroupAgentSchema).superRefine((roster, context) => {
    const coordinators = roster.filter((agent) => agent.role === 'coordinator');
    const verifiers = roster.filter((agent) => agent.role === 'verifier');
    if (coordinators.length !== 1) {
      context.addIssue({ code: z.ZodIssueCode.custom, message: 'exactly one Coordinator is required' });
    }
    if (verifiers.length !== 1) {
      context.addIssue({ code: z.ZodIssueCode.custom, message: 'exactly one Verifier is required' });
    }
  }),
  course_bindings: z.array(GroupCourseBindingSchema),
  source_links: z.array(GroupSourceLinkSchema),
  budget: z.number().int().nonnegative().nullable(),
  model_overrides: z.record(z.string()),
  tool_overrides: z.record(z.array(z.string())),
  created_at: z.string(),
  updated_at: z.string(),
  archived_at: z.string().nullable(),
  /** Runtime adapter only; never use as domain identity. */
  legacy_project_id: z.string().nullable(),
}).superRefine((group, context) => {
  const crossesBoundary = [
    ...group.roster.map((agent) => agent.group_id),
    ...group.course_bindings.map((binding) => binding.group_id),
    ...group.source_links.map((link) => link.group_id),
  ].some((ownerId) => ownerId !== group.group_id);
  if (crossesBoundary) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      message: 'nested Group state crosses the group isolation boundary',
    });
  }
});

export const GroupRuntimeSummarySchema = z.object({
  group_id: GroupIdSchema,
  lifecycle: GroupLifecycleSchema,
  active_agent_count: z.number().int().nonnegative(),
  course_binding_count: z.number().int().nonnegative(),
  source_count: z.number().int().nonnegative(),
  shared_source_count: z.number().int().nonnegative(),
  budget: z.number().int().nonnegative().nullable(),
  updated_at: z.string(),
});

export const GroupDeletePreviewSchema = z.object({
  group_id: GroupIdSchema,
  confirmation_token: z.string().regex(/^[0-9a-f]{64}$/),
  agent_count: z.number().int().nonnegative(),
  course_binding_count: z.number().int().nonnegative(),
  source_link_count: z.number().int().nonnegative(),
  exclusive_source_ids: z.array(GroupSourceIdSchema),
  shared_source_ids: z.array(GroupSourceIdSchema),
  source_reference_counts: z.record(z.number().int().positive()),
});

export const GroupDeletionReceiptSchema = z.object({
  schema_version: z.literal(1),
  receipt_id: z.string().regex(/^gdr_[0-9a-f]{32}$/),
  group_id: GroupIdSchema,
  deleted_at: z.string(),
  removed_agent_count: z.number().int().nonnegative(),
  removed_course_binding_count: z.number().int().nonnegative(),
  removed_source_link_count: z.number().int().nonnegative(),
  deleted_source_ids: z.array(GroupSourceIdSchema),
  retained_shared_source_ids: z.array(GroupSourceIdSchema),
}).strict();

/** Migration-window envelope. At least one identity is required; group_id is authoritative. */
export const GroupTransportIdentitySchema = z.object({
  group_id: GroupIdSchema.optional(),
  project_id: z.string().min(1).optional(),
}).refine((value) => !!value.group_id || !!value.project_id, {
  message: 'group_id or legacy project_id is required',
});

export function withGroupTransportAlias(
  groupId: GroupId,
  legacyProjectId?: string | null,
): { group_id: GroupId; project_id?: string } {
  return legacyProjectId
    ? { group_id: groupId, project_id: legacyProjectId }
    : { group_id: groupId };
}

export type GroupId = z.infer<typeof GroupIdSchema>;
export type GroupTemplate = z.infer<typeof GroupTemplateSchema>;
export type GroupLifecycle = z.infer<typeof GroupLifecycleSchema>;
export type ExpertTemplateConfig = z.infer<typeof ExpertTemplateConfigSchema>;
export type GroupAgent = z.infer<typeof GroupAgentSchema>;
export type GroupCourseBinding = z.infer<typeof GroupCourseBindingSchema>;
export type GroupSourceLink = z.infer<typeof GroupSourceLinkSchema>;
export type Group = z.infer<typeof GroupSchema>;
export type GroupRuntimeSummary = z.infer<typeof GroupRuntimeSummarySchema>;
export type GroupDeletePreview = z.infer<typeof GroupDeletePreviewSchema>;
export type GroupDeletionReceipt = z.infer<typeof GroupDeletionReceiptSchema>;
