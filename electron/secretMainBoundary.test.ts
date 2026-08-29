import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';


const source = (relativePath: string): string => readFileSync(
  join(process.cwd(), relativePath),
  'utf8',
);

describe('main-only OS credential boundary', () => {
  it('keeps safeStorage and the independent broker credential in Electron main', () => {
    const main = source('electron/main.ts');
    const preload = source('electron/preload.ts');
    const bridge = source('src/shared/bridge.ts');

    expect(main).toMatch(/\bsafeStorage\b/);
    expect(main).toContain('new SecretVault');
    expect(main).toContain('registerSecretIpc');
    expect(main).toContain('KNOWE_SECRET_BROKER_TOKEN: secretBrokerToken');
    expect(main).toContain("'X-Knowe-Secret-Broker-Token': secretBrokerToken");
    expect(main).toContain('/internal/credential-lease');

    expect(preload).not.toContain('safeStorage');
    expect(preload).not.toContain('secretBrokerToken');
    expect(preload).not.toContain('KNOWE_SECRET_BROKER_TOKEN');
    expect(bridge).not.toContain('secretBrokerToken');
    expect(bridge).not.toContain('KNOWE_SECRET_BROKER_TOKEN');
  });
});
