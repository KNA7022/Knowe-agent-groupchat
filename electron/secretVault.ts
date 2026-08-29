import { randomBytes } from 'node:crypto';

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
  kind: string;
  label: string;
  purpose: string;
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
const PURPOSE_RE = /^(?:provider|master-key):[a-z0-9][a-z0-9_.-]{0,63}$/;
const KIND_RE = /^(?:provider_credential|master_key)$/;
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
  kind: record.kind,
  label: record.label,
  purpose: record.purpose,
  created_at: record.created_at,
  schema_version: 1,
});

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
    this.records = Object.fromEntries(Object.entries(loaded).filter(([key, value]) => (
      SECRET_REF_RE.test(key)
      && value.secret_ref === key
      && KIND_RE.test(value.kind)
      && PURPOSE_RE.test(value.purpose)
      && typeof value.encrypted_base64 === 'string'
      && value.schema_version === 1
    )));
  }

  async store(input: SecretStoreRequest): Promise<SecretRef> {
    if (!this.safeStorage.isEncryptionAvailable()) {
      throw new Error('OS credential encryption is unavailable');
    }
    const kind = cleanText(input.kind, 64);
    const label = cleanText(input.label, 120);
    const purpose = cleanText(input.purpose, 80);
    if (!KIND_RE.test(kind) || !PURPOSE_RE.test(purpose)) {
      throw new Error('secret kind or purpose is not authorized');
    }
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
      kind,
      label,
      purpose,
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

  async issueLease(input: CredentialLeaseRequest & {
    deliver(lease: PrivateCredentialLease): Promise<void>;
  }): Promise<CredentialLeaseRef> {
    if (!this.safeStorage.isEncryptionAvailable()) {
      throw new Error('OS credential encryption is unavailable');
    }
    const record = this.records[input.secret_ref];
    if (!record) throw new Error('secret reference is unavailable');
    if (input.purpose !== record.purpose || !PURPOSE_RE.test(input.purpose)) {
      throw new Error('secret lease purpose mismatch');
    }
    if (!Number.isInteger(input.ttl_seconds)
        || input.ttl_seconds < 1
        || input.ttl_seconds > MAX_LEASE_TTL_SECONDS) {
      throw new Error('secret lease TTL is outside policy');
    }
    const lease: CredentialLeaseRef = {
      lease_id: `lease_${randomBytes(16).toString('hex')}`,
      secret_ref: record.secret_ref,
      purpose: record.purpose,
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
