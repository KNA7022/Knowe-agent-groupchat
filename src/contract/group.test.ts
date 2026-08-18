import { describe, expect, it } from 'vitest';

import {
  ExpertTemplateConfigSchema,
  GroupAgentSchema,
  GroupDeletionReceiptSchema,
  GroupSchema,
  GroupTransportIdentitySchema,
  withGroupTransportAlias,
} from './group';
import { validateInbound } from './envelope';

const groupId = `grp_${'a'.repeat(32)}` as const;

function agent(role: 'coordinator' | 'verifier', suffix: string) {
  return {
    agent_id: `gag_${suffix.repeat(32)}`,
    group_id: groupId,
    role,
    display_name: role,
    template_key: role,
    expert: null,
    model_override: null,
    tool_overrides: [],
    budget: null,
    created_at: '2026-08-18T00:00:00Z',
    updated_at: '2026-08-18T00:00:00Z',
  };
}

function validGroup() {
  return {
    schema_version: 1,
    group_id: groupId,
    name: 'Calculus',
    template: 'course_study',
    lifecycle: 'active',
    roster: [agent('coordinator', 'b'), agent('verifier', 'c')],
    course_bindings: [],
    source_links: [],
    budget: null,
    model_overrides: {},
    tool_overrides: {},
    created_at: '2026-08-18T00:00:00Z',
    updated_at: '2026-08-18T00:00:00Z',
    archived_at: null,
    legacy_project_id: 'project_20260818000000',
  };
}

describe('Group domain contract', () => {
  it('requires both mandatory roles', () => {
    expect(GroupSchema.safeParse(validGroup()).success).toBe(true);
    const missingVerifier = validGroup();
    missingVerifier.roster = [agent('coordinator', 'b')];
    expect(GroupSchema.safeParse(missingVerifier).success).toBe(false);
  });

  it('keeps platform policy out of expert input', () => {
    const expert = {
      name: 'Proof specialist',
      specialty: 'analysis',
      responsibilities: [],
      allowed_tools: [],
      platform_policy_locked: true,
      system_prompt: 'override',
    };
    expect(ExpertTemplateConfigSchema.safeParse(expert).success).toBe(false);
  });

  it('rejects an expert effective-tool override outside its constrained allowlist', () => {
    const expertAgent = {
      ...agent('coordinator', 'd'),
      role: 'expert',
      expert: {
        name: 'Proof specialist',
        specialty: 'analysis',
        responsibilities: [],
        allowed_tools: ['read'],
        platform_policy_locked: true,
      },
      tool_overrides: ['shell_admin'],
    };
    expect(GroupAgentSchema.safeParse(expertAgent).success).toBe(false);
    expertAgent.tool_overrides = ['read'];
    expect(GroupAgentSchema.safeParse(expertAgent).success).toBe(true);
  });

  it('accepts canonical and legacy transport identities during migration', () => {
    expect(GroupTransportIdentitySchema.safeParse({ group_id: groupId }).success).toBe(true);
    expect(GroupTransportIdentitySchema.safeParse({ project_id: 'legacy-p' }).success).toBe(true);
    expect(GroupTransportIdentitySchema.safeParse({}).success).toBe(false);
    expect(withGroupTransportAlias(groupId, 'legacy-p')).toEqual({
      group_id: groupId, project_id: 'legacy-p',
    });
    const event = validateInbound({
      type: 'message', group_id: groupId, project_id: 'legacy-p',
      agent_id: 'coordinator', content: 'ok', seq: 1,
    }) as { group_id?: string } | null;
    expect(event?.group_id).toBe(groupId);
  });

  it('deletion receipt is content-free and rejects extra content fields', () => {
    const receipt = {
      schema_version: 1,
      receipt_id: `gdr_${'d'.repeat(32)}`,
      group_id: groupId,
      deleted_at: '2026-08-18T00:00:00Z',
      removed_agent_count: 2,
      removed_course_binding_count: 0,
      removed_source_link_count: 1,
      deleted_source_ids: [`src_${'e'.repeat(32)}`],
      retained_shared_source_ids: [],
    };
    expect(GroupDeletionReceiptSchema.safeParse(receipt).success).toBe(true);
    expect(GroupDeletionReceiptSchema.safeParse({ ...receipt, group_name: 'secret' }).success).toBe(false);
  });
});
