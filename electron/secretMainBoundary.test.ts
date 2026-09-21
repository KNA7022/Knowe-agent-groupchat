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
    // broker token 现在经 `backendEnv.ts` 的 `buildRuntimeEnv` 注入；main.ts 只负责
    // 把值传进去（重构后字面量 `KNOWE_SECRET_BROKER_TOKEN: secretBrokerToken` 不再
    // 出现在 main.ts 里，所以这里改断言"传递"这一事实 + 新模块里的键名）。
    expect(main).toContain('secretBrokerToken,');
    expect(main).toContain('buildRuntimeEnv(');
    expect(source('electron/backendEnv.ts')).toContain('KNOWE_SECRET_BROKER_TOKEN: options.secretBrokerToken');
    expect(main).toContain("'X-Knowe-Secret-Broker-Token': secretBrokerToken");
    expect(main).toContain('/internal/credential-lease');

    expect(preload).not.toContain('safeStorage');
    expect(preload).not.toContain('secretBrokerToken');
    expect(preload).not.toContain('KNOWE_SECRET_BROKER_TOKEN');
    expect(bridge).not.toContain('secretBrokerToken');
    expect(bridge).not.toContain('KNOWE_SECRET_BROKER_TOKEN');
  });

  it('builds the backend environment through the extracted pure module', () => {
    const main = source('electron/main.ts');

    // I-R6：旧实现是 `{...process.env}` 全量复制 + 7 类**后缀**黑名单，实测漏掉
    // AZURE_OPENAI_KEY / OPENAI_API_TOKEN / AWS_SECRET_ACCESS_KEY /
    // GOOGLE_APPLICATION_CREDENTIALS / HF_TOKEN / GITHUB_TOKEN 等一大批常见凭据名。
    //
    // 这里只断言**接线**：main.ts 必须把宿主环境交给抽出去的纯函数去筛。
    // 筛选逻辑本身由 `backendEnv.test.ts` **真实执行**验证（原先这里是从源码抠
    // 数组字面量做静态断言，观察不到任何行为，连 `ComSpec` 因大小写传不出去都发现不了）。
    expect(main).toContain('selectInheritedEnv(process.env');
    expect(main).toContain('buildRuntimeEnv(');

    // 反向断言：后缀黑名单这条路必须彻底消失（它证明不了什么，只会给人安全感）。
    expect(main).not.toMatch(/delete env\[inheritedName\]/);
    expect(main).not.toMatch(/API_KEY\|ACCESS_TOKEN\|REFRESH_TOKEN\|AUTH_TOKEN/);
    expect(main).toContain('/internal/credential-revoke');
  });

  it('keeps the networking and TLS variables the backend needs', () => {
    // allowlist 已移到 backendEnv.ts（为的是能被真实执行）；这里从该模块读取。
    const env = source('electron/backendEnv.ts');
    const block = env.match(/const BACKEND_ENV_ALLOWLIST = \[([\s\S]*?)\] as const;/);
    const allowed = [...(block?.[1] ?? '').matchAll(/'([^']+)'/g)]
      .map((m) => m[1])
      .filter((name): name is string => typeof name === 'string');
    expect(block, '必须能从 backendEnv.ts 提取出 BACKEND_ENV_ALLOWLIST').not.toBeNull();

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

  it('matches the allowlist case-insensitively so Windows names survive', () => {
    const main = source('electron/main.ts');

    // 顶层契约仍在 main.ts 里：它必须调用抽出去的纯函数。
    expect(main).toContain('selectInheritedEnv(process.env');
    expect(main).toContain('buildRuntimeEnv(');
  });

  it('refuses to inherit long-lived cloud credentials even though the names vary', () => {
    const env = source('electron/backendEnv.ts');

    // 直接解析 allowlist 的内容，而不是拿整个文件做子串匹配：
    // 文件里（注释中）会正当地提到这些凭据名作为反例，整文件匹配会误报。
    const block = env.match(/const BACKEND_ENV_ALLOWLIST = \[([\s\S]*?)\] as const;/);
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
