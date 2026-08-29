import { afterEach, describe, expect, it, vi } from 'vitest';

import { SecretVault, type SafeStorageAdapter } from './secretVault';
import { registerSecretIpc, type SecretIpcAdapter } from './secretIpc';


const memorySafeStorage = (): SafeStorageAdapter => ({
  isEncryptionAvailable: () => true,
  encryptString: (value: string) => Buffer.from(`encrypted:${value}`, 'utf8'),
  decryptString: (value: Buffer) => value.toString('utf8').replace(/^encrypted:/, ''),
});

const providerInput = (value: string) => ({
  slot: 'main',
  provider_id: 'deepseek',
  endpoint: 'https://api.deepseek.com/v1',
  transport: 'openai_chat',
  value,
});

describe('SecretVault security boundary', () => {
  afterEach(() => { vi.restoreAllMocks(); });

  it('stores only encrypted blobs and returns metadata, never durable secret bytes', async () => {
    const writes: unknown[] = [];
    const vault = new SecretVault({
      safeStorage: memorySafeStorage(),
      load: () => ({}),
      save: (value) => { writes.push(value); },
      now: () => new Date('2026-08-29T00:00:00.000Z'),
    });
    const ref = await vault.store(providerInput('sk-renderer-input'));
    expect(JSON.stringify(writes)).not.toContain('sk-renderer-input');
    expect(JSON.stringify(ref)).not.toContain('encrypted:');
    expect(vault.get(ref.secret_ref)).toEqual(ref);
  });

  it('issues purpose-bound short leases only to the trusted backend delivery callback', async () => {
    const delivered: unknown[] = [];
    const vault = new SecretVault({
      safeStorage: memorySafeStorage(),
      load: () => ({}),
      save: () => undefined,
      now: () => new Date('2026-08-29T00:00:00.000Z'),
    });
    const ref = await vault.store(providerInput('sk-value'));
    const lease = await vault.issueLease({
      secret_ref: ref.secret_ref,
      ttl_seconds: 30,
      deliver: async (privateLease: { purpose: string }) => { delivered.push(privateLease); },
    });
    expect(JSON.stringify(lease)).not.toContain('sk-value');
    expect(JSON.stringify(delivered)).toContain('sk-value');
    const forged = await vault.issueLease({
      secret_ref: ref.secret_ref,
      purpose: 'diagnostics',
      ttl_seconds: 30,
      deliver: async (privateLease: { purpose: string }) => { delivered.push(privateLease); },
    } as never);
    expect(forged.purpose).toBe(ref.purpose);
    expect(forged.kind).toBe('provider_credential');
    expect(forged.audience).toBe('provider_gateway');
    await expect(vault.issueLease({
      secret_ref: ref.secret_ref,
      ttl_seconds: 301,
      deliver: async () => undefined,
    })).rejects.toThrow(/ttl/i);
  });

  it('rejects arbitrary renderer reads and never exposes a plaintext lease over IPC', async () => {
    const handlers = new Map<string, (...args: any[]) => unknown>();
    const ipc: SecretIpcAdapter = {
      handle: (channel, handler) => { handlers.set(channel, handler); },
      removeHandler: (channel) => { handlers.delete(channel); },
    };
    const vault = new SecretVault({
      safeStorage: memorySafeStorage(), load: () => ({}), save: () => undefined,
    });
    const ref = await vault.store(providerInput('sk-ipc-secret'));
    const revokeLease = vi.fn().mockResolvedValue(undefined);
    registerSecretIpc({
      ipc,
      vault,
      trustedWebContentsId: () => 7,
      deliverLease: async () => undefined,
      revokeLease,
    });
    const fakeEvent = (id: number) => ({ sender: { id } });
    const get = handlers.get('knowe:secrets:get')!;
    const lease = handlers.get('knowe:secrets:lease')!;
    const remove = handlers.get('knowe:secrets:delete')!;
    await expect(Promise.resolve(get(fakeEvent(8), ref.secret_ref))).rejects.toThrow(/trusted renderer/i);
    await expect(Promise.resolve(lease(fakeEvent(8), {
      secret_ref: ref.secret_ref, ttl_seconds: 30,
    }))).rejects.toThrow(/trusted renderer/i);
    const publicLease = await lease(fakeEvent(7), {
      secret_ref: ref.secret_ref, ttl_seconds: 30,
    });
    expect(JSON.stringify(publicLease)).not.toContain('sk-ipc-secret');
    await remove(fakeEvent(7), ref.secret_ref);
    expect(revokeLease).toHaveBeenCalledWith(ref.secret_ref);
    expect(vault.get(ref.secret_ref)).toBeNull();
  });

  it('fails closed when OS credential encryption is unavailable', async () => {
    let persisted: Record<string, unknown> = {};
    const available = new SecretVault({
      safeStorage: memorySafeStorage(),
      load: () => ({}),
      save: (value) => { persisted = value; },
    });
    const ref = await available.store(providerInput('sk-value'));
    const unavailable: SafeStorageAdapter = {
      isEncryptionAvailable: () => false,
      encryptString: () => { throw new Error('must not encrypt'); },
      decryptString: () => { throw new Error('must not decrypt'); },
    };
    const vault = new SecretVault({
      safeStorage: unavailable,
      load: () => persisted as never,
      save: () => { throw new Error('must not persist'); },
    });

    await expect(vault.store(providerInput('sk-other'))).rejects.toThrow(/encryption.*unavailable/i);
    await expect(vault.issueLease({
      secret_ref: ref.secret_ref,
      ttl_seconds: 30,
      deliver: async () => { throw new Error('must not deliver'); },
    })).rejects.toThrow(/encryption.*unavailable/i);
  });

  it('derives immutable provider metadata in main instead of accepting renderer purpose or kind', async () => {
    const vault = new SecretVault({
      safeStorage: memorySafeStorage(), load: () => ({}), save: () => undefined,
      now: () => new Date('2026-08-29T00:00:00.000Z'),
    });
    const ref = await vault.store({
      slot: 'main',
      provider_id: 'deepseek',
      endpoint: 'https://API.DeepSeek.com:443/v1/',
      transport: 'openai_chat',
      value: 'sk-main-derived',
    } as never);
    expect(ref).toMatchObject({
      slot: 'main',
      kind: 'provider_credential',
      audience: 'provider_gateway',
      provider_id: 'deepseek',
      endpoint: 'https://api.deepseek.com/v1',
      transport: 'openai_chat',
    });
    expect(ref.purpose).toMatch(/^provider:[0-9a-f]{64}$/);
    await expect(vault.store({
      slot: 'main', provider_id: 'deepseek', endpoint: 'http://api.deepseek.com/v1',
      transport: 'openai_chat', value: 'sk-no-http',
    } as never)).rejects.toThrow(/https/i);
  });

  it('rejects a partially corrupt vault instead of silently filtering records', () => {
    expect(() => new SecretVault({
      safeStorage: memorySafeStorage(),
      load: () => ({
        [`secret_${'a'.repeat(32)}`]: {
          secret_ref: `secret_${'a'.repeat(32)}`,
          kind: 'provider_credential',
          label: 'old',
          purpose: 'provider:main',
          created_at: '2026-08-29T00:00:00.000Z',
          encrypted_base64: '%%%not-base64%%%',
          schema_version: 1,
        },
      } as never),
      save: () => undefined,
    })).toThrow(/vault|record|corrupt/i);
  });

  it('revokes and removes a replaced slot only after the new ref becomes authoritative', async () => {
    let now = new Date('2026-08-29T00:00:00.000Z');
    const vault = new SecretVault({
      safeStorage: memorySafeStorage(), load: () => ({}), save: () => undefined,
      now: () => now,
    });
    const previous = await vault.store(providerInput('sk-previous'));
    now = new Date('2026-08-29T00:01:00.000Z');
    const active = await vault.store(providerInput('sk-active'));
    const revoke = vi.fn().mockResolvedValue(undefined);

    const removed = await vault.reconcileActive(new Set([active.secret_ref]), revoke);

    expect(removed).toEqual([previous.secret_ref]);
    expect(revoke).toHaveBeenCalledWith(previous.secret_ref);
    expect(vault.get(previous.secret_ref)).toBeNull();
    expect(vault.get(active.secret_ref)).toEqual(active);
  });

  it('keeps durable ciphertext when backend revocation fails', async () => {
    let now = new Date('2026-08-29T00:00:00.000Z');
    const vault = new SecretVault({
      safeStorage: memorySafeStorage(), load: () => ({}), save: () => undefined,
      now: () => now,
    });
    const previous = await vault.store(providerInput('sk-previous'));
    now = new Date('2026-08-29T00:01:00.000Z');
    const active = await vault.store(providerInput('sk-active'));

    await expect(vault.reconcileActive(
      new Set([active.secret_ref]),
      async () => { throw new Error('broker offline'); },
    )).rejects.toThrow(/broker offline/);
    expect(vault.get(previous.secret_ref)).toEqual(previous);
  });
});
