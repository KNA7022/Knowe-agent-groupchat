import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  prepareBindingWireForBackend,
  useSettingsStore,
  type ModelBinding,
} from './settings';


const binding = (): ModelBinding => ({
  provider: 'openai-api',
  model: 'gpt-test',
  apiKey: 'sk-renderer-input',
  hasApiKey: false,
  clearApiKey: false,
  baseUrl: 'https://provider.example/v1',
  sealed: true,
});

const providerPurpose = `provider:${'1'.repeat(64)}`;
const secretMetadata = (secretRef: string) => ({
  secret_ref: secretRef,
  slot: 'main',
  kind: 'provider_credential' as const,
  label: 'Provider credential (main)',
  purpose: providerPurpose,
  audience: 'provider_gateway' as const,
  provider_id: 'openai-api',
  endpoint: 'https://provider.example/v1',
  transport: 'codex_responses',
  created_at: '2026-08-29T00:00:00.000Z',
  schema_version: 1 as const,
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
  delete (window as unknown as { knowe?: unknown }).knowe;
});

describe('provider credential settings boundary', () => {
  it('stores through main and sends Python only an opaque purpose-bound handle', async () => {
    const stored = secretMetadata(`secret_${'a'.repeat(32)}`);
    const storeSecret = vi.fn().mockResolvedValue(stored);
    const getSecret = vi.fn().mockResolvedValue(stored);
    const leaseSecret = vi.fn().mockResolvedValue({
      lease_id: `lease_${'b'.repeat(32)}`,
      ...stored,
      expires_at: '2026-08-29T00:05:00.000Z',
    });
    (window as unknown as { knowe: unknown }).knowe = { storeSecret, getSecret, leaseSecret };

    const wire = await prepareBindingWireForBackend(
      binding(), 'provider:main', 'Main provider credential',
    );
    expect(storeSecret).toHaveBeenCalledWith({
      slot: 'main', provider_id: 'openai-api', endpoint: 'https://provider.example/v1',
      transport: 'codex_responses', value: 'sk-renderer-input',
    });
    expect(leaseSecret).toHaveBeenCalledWith({
      secret_ref: `secret_${'a'.repeat(32)}`, ttl_seconds: 300,
    });
    expect(wire).toMatchObject({
      secret_ref: `secret_${'a'.repeat(32)}`,
      credential_purpose: providerPurpose,
      credential_slot: 'main',
      credential_kind: 'provider_credential',
      credential_audience: 'provider_gateway',
    });
    expect(JSON.stringify(wire)).not.toContain('sk-renderer-input');
    expect(wire).not.toHaveProperty('api_key');
    expect(wire).not.toHaveProperty('lease_id');
  });

  it('reuses one encrypted SecretRef across test and apply for the same credential scope', async () => {
    const secretRef = `secret_${'d'.repeat(32)}`;
    const stored = secretMetadata(secretRef);
    const storeSecret = vi.fn().mockResolvedValue(stored);
    const getSecret = vi.fn().mockResolvedValue(stored);
    const leaseSecret = vi.fn().mockResolvedValue({
      lease_id: `lease_${'e'.repeat(32)}`,
      ...stored,
      expires_at: '2026-08-29T00:05:00.000Z',
    });
    (window as unknown as { knowe: unknown }).knowe = { storeSecret, getSecret, leaseSecret };

    const scoped = { ...binding(), apiKey: 'sk-test-apply-input' };
    const tested = await prepareBindingWireForBackend(
      scoped, 'provider:main', 'Main provider credential',
    );
    const applied = await prepareBindingWireForBackend(
      scoped, 'provider:main', 'Main provider credential',
    );

    expect(storeSecret).toHaveBeenCalledTimes(1);
    expect(tested?.secret_ref).toBe(secretRef);
    expect(applied?.secret_ref).toBe(secretRef);
    expect(leaseSecret).toHaveBeenCalledTimes(2);
  });

  it('fails closed when the Electron secret bridge is unavailable', async () => {
    await expect(prepareBindingWireForBackend(
      binding(), 'provider:main', 'Main provider credential',
    )).rejects.toThrow(/secret storage|unavailable/i);
  });

  it('deletes the durable OS-encrypted credential when a binding is cleared', async () => {
    const deleteSecret = vi.fn().mockResolvedValue(true);
    (window as unknown as { knowe: unknown }).knowe = {
      storeSecret: vi.fn(),
      leaseSecret: vi.fn(),
      deleteSecret,
    };
    const cleared = {
      ...binding(),
      apiKey: '',
      clearApiKey: true,
      secretRef: `secret_${'c'.repeat(32)}`,
    };

    const wire = await prepareBindingWireForBackend(
      cleared, 'provider:main', 'Main provider credential',
    );

    expect(deleteSecret).toHaveBeenCalledWith(`secret_${'c'.repeat(32)}`);
    expect(wire).toMatchObject({ clear_api_key: true });
    expect(wire).not.toHaveProperty('secret_ref');
  });

  it('retains the opaque prior ref only long enough for the clear handoff', async () => {
    vi.useFakeTimers();
    const secretRef = `secret_${'f'.repeat(32)}`;
    const current = {
      ...binding(),
      apiKey: '',
      hasApiKey: true,
      secretRef,
      credentialPurpose: 'provider:main',
    };
    useSettingsStore.setState({ mainModel: current });
    useSettingsStore.getState().saveMainModel({
      provider: current.provider,
      model: current.model,
      apiKey: '',
      hasApiKey: false,
      clearApiKey: true,
    });
    const clearing = useSettingsStore.getState().mainModel;
    expect(clearing).toMatchObject({ clearApiKey: true, secretRef });

    const deleteSecret = vi.fn().mockResolvedValue(true);
    (window as unknown as { knowe: unknown }).knowe = {
      storeSecret: vi.fn(), leaseSecret: vi.fn(), deleteSecret,
    };
    await prepareBindingWireForBackend(
      clearing, 'provider:main', 'Main provider credential',
    );
    expect(deleteSecret).toHaveBeenCalledWith(secretRef);
  });
});
