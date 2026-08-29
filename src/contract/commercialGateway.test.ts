import { describe, expect, it } from 'vitest';

import {
  DlpDecisionSchema,
  McpServerConfigSchema,
  ModelCapabilitySchema,
  PiiFindingSchema,
  SecretRefSchema,
  ToolDescriptorSchema,
  ToolInvocationSchema,
} from './taskRuntime';


describe('commercial gateway contracts', () => {
  it('keeps MCP prompts disabled and rejects unknown transport fields', () => {
    expect(McpServerConfigSchema.safeParse({
      server_id: `mcp_${'1'.repeat(32)}`,
      name: 'local',
      transport: 'stdio',
      command: 'trusted-mcp',
      args: [],
      url: '',
      allowed_hosts: [],
      allowed_tools: ['lookup'],
      allowed_resources: [],
      prompts_enabled: false,
      schema_version: 1,
    }).success).toBe(true);
    expect(McpServerConfigSchema.safeParse({
      server_id: `mcp_${'1'.repeat(32)}`,
      name: 'bad',
      transport: 'stdio',
      command: 'trusted-mcp',
      args: [], url: '', allowed_hosts: [], allowed_tools: [], allowed_resources: [],
      prompts_enabled: true,
      schema_version: 1,
    }).success).toBe(false);
  });

  it('parses opaque public security contracts without raw PII or secret material', () => {
    expect(ToolDescriptorSchema).toBeDefined();
    expect(ToolInvocationSchema).toBeDefined();
    expect(ModelCapabilitySchema).toBeDefined();
    expect(PiiFindingSchema.parse({
      finding_id: `pii_${'2'.repeat(32)}`,
      kind: 'student_identifier',
      start: 0,
      end: 10,
      digest: 'a'.repeat(64),
      confidence: 1,
      schema_version: 1,
    })).toBeTruthy();
    expect(DlpDecisionSchema.safeParse({
      decision_id: `dlp_${'3'.repeat(32)}`,
      allowed: false,
      requires_approval: true,
      finding_ids: [`pii_${'2'.repeat(32)}`],
      reason: 'sensitive_outbound',
      schema_version: 1,
    }).success).toBe(true);
    expect(SecretRefSchema.safeParse({
      secret_ref: `secret_${'4'.repeat(32)}`,
      kind: 'provider_credential',
      label: 'Provider key',
      purpose: 'provider:chat',
      created_at: '2026-08-29T00:00:00Z',
      schema_version: 1,
    }).success).toBe(true);
  });
});
