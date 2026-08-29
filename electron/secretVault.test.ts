import { afterEach, describe, expect, it, vi } from 'vitest';

import { SecretVault, type SafeStorageAdapter } from './secretVault';
import { registerSecretIpc, type SecretIpcAdapter } from './secretIpc';


const memorySafeStorage = (): SafeStorageAdapter => ({
  isEncryptionAvailable: () => true,
  encryptString: (value: string) => Buffer.from(`encrypted:${value}`, 'utf8'),
  decryptString: (value: Buffer) => value.toString('utf8').replace(/^encrypted:/, ''),
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
    const ref = await vault.store({
      label: 'Provider key',
      kind: 'provider_credential',
      purpose: 'provider:chat',
      value: 'sk-renderer-input',
    });
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
    const ref = await vault.store({
      label: 'Provider key', kind: 'provider_credential', purpose: 'provider:chat', value: 'sk-value',
    });
    const lease = await vault.issueLease({
      secret_ref: ref.secret_ref,
      purpose: 'provider:chat',
      ttl_seconds: 30,
      deliver: async (privateLease) => { delivered.push(privateLease); },
    });
    expect(JSON.stringify(lease)).not.toContain('sk-value');
    expect(JSON.stringify(delivered)).toContain('sk-value');
    await expect(vault.issueLease({
      secret_ref: ref.secret_ref,
      purpose: 'diagnostics',
      ttl_seconds: 30,
      deliver: async () => undefined,
    })).rejects.toThrow(/purpose/i);
    await expect(vault.issueLease({
      secret_ref: ref.secret_ref,
      purpose: 'provider:chat',
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
    const ref = await vault.store({
      label: 'Key', kind: 'provider_credential', purpose: 'provider:chat', value: 'sk-ipc-secret',
    });
    registerSecretIpc({
      ipc,
      vault,
      trustedWebContentsId: () => 7,
      deliverLease: async () => undefined,
    });
    const fakeEvent = (id: number) => ({ sender: { id } });
    const get = handlers.get('knowe:secrets:get')!;
    const lease = handlers.get('knowe:secrets:lease')!;
    await expect(Promise.resolve(get(fakeEvent(8), ref.secret_ref))).rejects.toThrow(/trusted renderer/i);
    await expect(Promise.resolve(lease(fakeEvent(8), {
      secret_ref: ref.secret_ref, purpose: 'provider:chat', ttl_seconds: 30,
    }))).rejects.toThrow(/trusted renderer/i);
    const publicLease = await lease(fakeEvent(7), {
      secret_ref: ref.secret_ref, purpose: 'provider:chat', ttl_seconds: 30,
    });
    expect(JSON.stringify(publicLease)).not.toContain('sk-ipc-secret');
  });

  it('fails closed when OS credential encryption is unavailable', async () => {
    let persisted: Record<string, unknown> = {};
    const available = new SecretVault({
      safeStorage: memorySafeStorage(),
      load: () => ({}),
      save: (value) => { persisted = value; },
    });
    const ref = await available.store({
      label: 'Key', kind: 'provider_credential', purpose: 'provider:chat', value: 'sk-value',
    });
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

    await expect(vault.store({
      label: 'Other', kind: 'provider_credential', purpose: 'provider:chat', value: 'sk-other',
    })).rejects.toThrow(/encryption.*unavailable/i);
    await expect(vault.issueLease({
      secret_ref: ref.secret_ref,
      purpose: 'provider:chat',
      ttl_seconds: 30,
      deliver: async () => { throw new Error('must not deliver'); },
    })).rejects.toThrow(/encryption.*unavailable/i);
  });
});
