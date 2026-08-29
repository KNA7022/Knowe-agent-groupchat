import type { SecretVault } from './secretVault';
import {
  IPC,
  type CredentialLeaseRequest,
  type CredentialLeaseRef,
  type SecretStoreRequest,
} from '../src/shared/bridge';


type IpcEvent = Readonly<{ sender: Readonly<{ id: number }> }>;
type IpcHandler = (event: IpcEvent, ...args: any[]) => unknown;

export interface SecretIpcAdapter {
  handle(channel: string, handler: IpcHandler): void;
  removeHandler(channel: string): void;
}

type RegisterSecretIpcOptions = Readonly<{
  ipc: SecretIpcAdapter;
  vault: SecretVault;
  trustedWebContentsId(): number | null;
  deliverLease(lease: CredentialLeaseRef & { value: string }): Promise<void>;
  revokeLease(secretRef: string): Promise<void>;
}>;

export function registerSecretIpc(options: RegisterSecretIpcOptions): () => void {
  const trusted = (event: IpcEvent): void => {
    const expected = options.trustedWebContentsId();
    if (expected == null || event.sender.id !== expected) {
      throw new Error('secret IPC requires the trusted renderer');
    }
  };
  options.ipc.handle(IPC.secretStore, async (event, input: SecretStoreRequest) => {
    trusted(event);
    return options.vault.store(input);
  });
  options.ipc.handle(IPC.secretGet, async (event, secretRef: string) => {
    trusted(event);
    return options.vault.get(secretRef);
  });
  options.ipc.handle(IPC.secretDelete, async (event, secretRef: string) => {
    trusted(event);
    if (options.vault.get(secretRef)) await options.revokeLease(secretRef);
    return options.vault.delete(secretRef);
  });
  options.ipc.handle(IPC.secretLease, async (event, input: CredentialLeaseRequest) => {
    trusted(event);
    return options.vault.issueLease({ ...input, deliver: options.deliverLease });
  });
  return () => {
    options.ipc.removeHandler(IPC.secretStore);
    options.ipc.removeHandler(IPC.secretGet);
    options.ipc.removeHandler(IPC.secretDelete);
    options.ipc.removeHandler(IPC.secretLease);
  };
}
