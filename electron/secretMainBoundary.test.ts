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

  it('passes only an explicit allowlist of environment variables to the backend', () => {
    const main = source('electron/main.ts');

    // I-R6：旧实现是 `{...process.env}` 全量复制 + 7 类**后缀**黑名单，实测漏掉
    // AZURE_OPENAI_KEY / OPENAI_API_TOKEN / AWS_SECRET_ACCESS_KEY /
    // GOOGLE_APPLICATION_CREDENTIALS / HF_TOKEN / GITHUB_TOKEN 等一大批常见凭据名，
    // 于是宿主环境里的长生命周期凭据会被继承给 Python 进程。
    // 现在改为显式 allowlist：进程只拿到 OS 运行必需变量 + 固定运行参数。
    expect(main).toContain('BACKEND_ENV_ALLOWLIST');
    expect(main).toContain('DEV_EXTRA_ENV_ALLOWLIST');
    expect(main).toContain('PASSTHROUGH_ENV');
    expect(main).toMatch(/allowed\.has\(name\)/);

    // 反向断言：后缀黑名单这条路必须彻底消失（它证明不了什么，只会给人安全感）。
    expect(main).not.toMatch(/delete env\[inheritedName\]/);
    expect(main).not.toMatch(/API_KEY\|ACCESS_TOKEN\|REFRESH_TOKEN\|AUTH_TOKEN/);
    expect(main).toContain('/internal/credential-revoke');
  });

  it('keeps the networking and TLS variables the backend needs', () => {
    const main = source('electron/main.ts');
    const block = main.match(/const BACKEND_ENV_ALLOWLIST = \[([\s\S]*?)\] as const;/);
    const allowed = [...(block?.[1] ?? '').matchAll(/'([^']+)'/g)]
      .map((m) => m[1])
      .filter((name): name is string => typeof name === 'string');

    // I6：白名单第一版只查"没有凭据名"，于是漏掉了代理/CA —— 需要代理访问
    // OpenAI/Gemini 的部署、以及装了企业 MITM CA 的环境会**直连并失败**。
    // 这里断言必需集合是白名单的子集，而不是反过来只做凭据名反查。
    for (const name of [
      'HTTPS_PROXY',
      'HTTP_PROXY',
      'NO_PROXY',
      'SSL_CERT_FILE',
      'REQUESTS_CA_BUNDLE',
      'SSL_CERT_DIR',
    ]) {
      expect(allowed, `allowlist 必须放行 ${name}`).toContain(name);
    }
  });

  it('refuses to inherit long-lived cloud credentials even though the names vary', () => {
    const main = source('electron/main.ts');

    // 直接解析 allowlist 的内容，而不是拿整个文件做子串匹配：
    // 文件里（注释中）会正当地提到这些凭据名作为反例，整文件匹配会误报。
    const block = main.match(/const BACKEND_ENV_ALLOWLIST = \[([\s\S]*?)\] as const;/);
    expect(block, '必须能提取出 BACKEND_ENV_ALLOWLIST').not.toBeNull();
    const allowed = [...(block?.[1] ?? '').matchAll(/'([^']+)'/g)]
      .map((m) => m[1])
      .filter((name): name is string => typeof name === 'string');

    // 提取型断言必须同时断言非空，否则空列表会让下面的检查空转通过。
    expect(allowed.length).toBeGreaterThan(5);
    expect(allowed).toContain('PATH');
    expect(allowed).toContain('SystemRoot');

    // 这些名字曾全部逃过后缀黑名单；allowlist 让它们天然不在集合里。
    for (const name of [
      'AZURE_OPENAI_KEY',
      'OPENAI_API_TOKEN',
      'AWS_SECRET_ACCESS_KEY',
      'GOOGLE_APPLICATION_CREDENTIALS',
      'HF_TOKEN',
      'GITHUB_TOKEN',
      'DEEPSEEK_API_KEY',
    ]) {
      expect(allowed, `allowlist 不应放行 ${name}`).not.toContain(name);
    }
    // 也不允许用通配/前缀把整类凭据放进来。
    expect(allowed.some((name) => /KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL/i.test(name))).toBe(false);
  });

  it('renews only backend-authoritative refs and fails closed when active state is unknown', () => {
    const main = source('electron/main.ts');
    expect(main).toContain('activeProviderSecretRefs()');
    expect(main).toContain('vault.reconcileActive(active, deliverCredentialRevocation)');
    expect(main).toContain('!active.has(reference.secret_ref)');
    expect(main).toMatch(/Unknown active state must not renew every historical record/);
  });
});
