import { createHash, randomBytes } from 'node:crypto';

import type {
  CredentialLeaseRequest,
  CredentialLeaseRef,
  SecretRef,
  SecretStoreRequest,
} from '../src/shared/bridge';


export interface SafeStorageAdapter {
  isEncryptionAvailable(): boolean;
  encryptString(value: string): Buffer;
  decryptString(value: Buffer): string;
}

type StoredSecret = Readonly<{
  secret_ref: string;
  slot: string;
  kind: 'provider_credential';
  label: string;
  purpose: string;
  audience: 'provider_gateway';
  provider_id: string;
  endpoint: string;
  transport: string;
  created_at: string;
  encrypted_base64: string;
  schema_version: 1;
}>;

export type SecretVaultFile = Record<string, StoredSecret>;

type SecretVaultOptions = Readonly<{
  safeStorage: SafeStorageAdapter;
  load(): SecretVaultFile;
  save(value: SecretVaultFile): void;
  now?: () => Date;
}>;

type PrivateCredentialLease = Readonly<CredentialLeaseRef & { value: string }>;

const SECRET_REF_RE = /^secret_[0-9a-f]{32}$/;
const PURPOSE_RE = /^provider:[0-9a-f]{64}$/;
const SLOT_RE = /^[a-z][a-z0-9_.:-]{0,127}$/;
const PROVIDER_RE = /^[a-z0-9][a-z0-9_.-]{0,63}$/;
const TRANSPORT_RE = /^(?:openai_chat|anthropic_messages|codex_responses)$/;
const MAX_LEASE_TTL_SECONDS = 300;

const cleanText = (value: unknown, maximum: number): string => {
  const text = String(value ?? '').trim();
  if (!text || text.length > maximum || /[\u0000-\u001f\u007f]/.test(text)) {
    throw new Error('secret metadata is invalid');
  }
  return text;
};

const publicRef = (record: StoredSecret): SecretRef => ({
  secret_ref: record.secret_ref,
  slot: record.slot,
  kind: record.kind,
  label: record.label,
  purpose: record.purpose,
  audience: record.audience,
  provider_id: record.provider_id,
  endpoint: record.endpoint,
  transport: record.transport,
  created_at: record.created_at,
  schema_version: 1,
});

const canonicalProviderEndpoint = (raw: unknown): string => {
  let parsed: URL;
  try {
    parsed = new URL(cleanText(raw, 2048));
  } catch {
    throw new Error('provider endpoint is invalid');
  }
  if (parsed.protocol !== 'https:' || parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new Error('provider endpoint requires HTTPS without credentials or query');
  }
  if (parsed.port === '443') parsed.port = '';
  const path = parsed.pathname.replace(/\/{2,}/g, '/').replace(/\/+$/, '');
  return `https://${parsed.hostname.toLowerCase()}${parsed.port ? `:${parsed.port}` : ''}${path}`;
};

const providerPurpose = (
  slot: string, providerId: string, endpoint: string, transport: string,
): string => {
  const body = [
    'studycrew-provider-credential-v1', slot, providerId, endpoint, transport, 'provider_gateway',
  ].join('\u0000');
  return `provider:${createHash('sha256').update(body, 'utf8').digest('hex')}`;
};

const validBase64 = (value: unknown): value is string => {
  if (typeof value !== 'string' || !value || value.length % 4 !== 0
      || !/^[A-Za-z0-9+/]*={0,2}$/.test(value)) return false;
  try {
    return Buffer.from(value, 'base64').toString('base64') === value;
  } catch {
    return false;
  }
};

const validateStoredSecret = (key: string, value: StoredSecret): void => {
  if (!value || typeof value !== 'object'
      || !SECRET_REF_RE.test(key) || value.secret_ref !== key
      || !SLOT_RE.test(value.slot)
      || value.kind !== 'provider_credential'
      || value.audience !== 'provider_gateway'
      || !PROVIDER_RE.test(value.provider_id)
      || !TRANSPORT_RE.test(value.transport)
      || value.endpoint !== canonicalProviderEndpoint(value.endpoint)
      || value.purpose !== providerPurpose(
        value.slot, value.provider_id, value.endpoint, value.transport,
      )
      || !PURPOSE_RE.test(value.purpose)
      || !value.label || value.label.length > 120
      || !Number.isFinite(Date.parse(value.created_at))
      || !validBase64(value.encrypted_base64)
      || value.schema_version !== 1) {
    throw new Error('encrypted credential vault contains a corrupt record');
  }
};

export class SecretVault {
  private readonly safeStorage: SafeStorageAdapter;
  private readonly persist: (value: SecretVaultFile) => void;
  private readonly now: () => Date;
  private records: SecretVaultFile;

  constructor(options: SecretVaultOptions) {
    this.safeStorage = options.safeStorage;
    this.persist = options.save;
    this.now = options.now ?? (() => new Date());
    const loaded = options.load();
    for (const [key, value] of Object.entries(loaded)) validateStoredSecret(key, value);
    this.records = { ...loaded };
  }

  async store(input: SecretStoreRequest): Promise<SecretRef> {
    if (!this.safeStorage.isEncryptionAvailable()) {
      throw new Error('OS credential encryption is unavailable');
    }
    const slot = cleanText(input.slot, 128).toLowerCase();
    const providerId = cleanText(input.provider_id, 64).toLowerCase();
    const endpoint = canonicalProviderEndpoint(input.endpoint);
    const transport = cleanText(input.transport, 64).toLowerCase();
    if (!SLOT_RE.test(slot) || !PROVIDER_RE.test(providerId) || !TRANSPORT_RE.test(transport)) {
      throw new Error('provider credential slot or binding is not authorized');
    }
    const purpose = providerPurpose(slot, providerId, endpoint, transport);
    const label = `Provider credential (${slot})`;
    if (typeof input.value !== 'string' || !input.value || input.value.length > 32_768) {
      throw new Error('secret value is invalid');
    }
    const secretRef = `secret_${randomBytes(16).toString('hex')}`;
    const encrypted = this.safeStorage.encryptString(input.value);
    if (!Buffer.isBuffer(encrypted) || encrypted.length === 0) {
      throw new Error('OS credential encryption failed');
    }
    const record: StoredSecret = {
      secret_ref: secretRef,
      slot,
      kind: 'provider_credential',
      label,
      purpose,
      audience: 'provider_gateway',
      provider_id: providerId,
      endpoint,
      transport,
      created_at: this.now().toISOString(),
      encrypted_base64: encrypted.toString('base64'),
      schema_version: 1,
    };
    const next = { ...this.records, [secretRef]: record };
    this.persist(next);
    this.records = next;
    return publicRef(record);
  }

  get(secretRef: string): SecretRef | null {
    if (!SECRET_REF_RE.test(String(secretRef))) throw new Error('secret reference is invalid');
    const record = this.records[secretRef];
    return record ? publicRef(record) : null;
  }

  delete(secretRef: string): boolean {
    if (!SECRET_REF_RE.test(String(secretRef))) throw new Error('secret reference is invalid');
    if (!this.records[secretRef]) return false;
    const next = { ...this.records };
    delete next[secretRef];
    this.persist(next);
    this.records = next;
    return true;
  }

  list(): SecretRef[] {
    return Object.values(this.records).map(publicRef);
  }

  async reconcileActive(
    activeRefs: ReadonlySet<string>,
    revoke: (secretRef: string) => Promise<void>,
  ): Promise<string[]> {
    const activeBySlot = new Map<string, StoredSecret>();
    for (const secretRef of activeRefs) {
      const record = this.records[secretRef];
      if (record) activeBySlot.set(record.slot, record);
    }
    const nowMs = this.now().getTime();
    const stale: string[] = [];
    for (const record of Object.values(this.records)) {
      if (activeRefs.has(record.secret_ref)) continue;
      const active = activeBySlot.get(record.slot);
      const recordMs = Date.parse(record.created_at);
      const activeMs = active ? Date.parse(active.created_at) : Number.NaN;
      if ((active && recordMs < activeMs) || nowMs - recordMs > 86_400_000) {
        stale.push(record.secret_ref);
      }
    }
    for (const secretRef of stale) await revoke(secretRef);
    if (stale.length) {
      const next = { ...this.records };
      for (const secretRef of stale) delete next[secretRef];
      this.persist(next);
      this.records = next;
    }
    return stale;
  }

  async issueLease(input: CredentialLeaseRequest & {
    deliver(lease: PrivateCredentialLease): Promise<void>;
  }): Promise<CredentialLeaseRef> {
    if (!this.safeStorage.isEncryptionAvailable()) {
      throw new Error('OS credential encryption is unavailable');
    }
    const record = this.records[input.secret_ref];
    if (!record) throw new Error('secret reference is unavailable');
    if (!Number.isInteger(input.ttl_seconds)
        || input.ttl_seconds < 1
        || input.ttl_seconds > MAX_LEASE_TTL_SECONDS) {
      throw new Error('secret lease TTL is outside policy');
    }
    const lease: CredentialLeaseRef = {
      lease_id: `lease_${randomBytes(16).toString('hex')}`,
      secret_ref: record.secret_ref,
      slot: record.slot,
      kind: record.kind,
      purpose: record.purpose,
      audience: record.audience,
      provider_id: record.provider_id,
      endpoint: record.endpoint,
      transport: record.transport,
      expires_at: new Date(this.now().getTime() + input.ttl_seconds * 1000).toISOString(),
    };
    const encrypted = Buffer.from(record.encrypted_base64, 'base64');
    let value = this.safeStorage.decryptString(encrypted);
    if (!value) throw new Error('OS credential decryption failed');
    try {
      await input.deliver({ ...lease, value });
    } finally {
      encrypted.fill(0);
      value = '';
    }
    return lease;
  }
}

export const redactSecretDiagnostics = (value: unknown): unknown => {
  if (Array.isArray(value)) return value.map(redactSecretDiagnostics);
  if (!value || typeof value !== 'object') return value;
  return Object.fromEntries(Object.entries(value as Record<string, unknown>).map(([key, item]) => {
    const folded = key.toLowerCase().replace(/-/g, '_');
    if (/(?:secret|credential|password|api_key|token|authorization|cookie|encrypted)/.test(folded)) {
      return [key, item == null || item === '' ? item : '[REDACTED]'];
    }
    return [key, redactSecretDiagnostics(item)];
  }));
};
