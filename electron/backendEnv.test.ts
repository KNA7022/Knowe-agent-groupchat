/**
 * backendEnv.test.ts — **真实执行**后端环境构造，而不是从源码里抠字面量。
 *
 * 为什么要单独一个文件：这段逻辑原先内联在 `main.ts`，而 `main.ts` 顶层就调用
 * electron 的 `app`，测试无法导入。之前的测试只断言"数组里有某个字符串"，因此
 * **观察不到任何真实行为**——连 `ComSpec` 因为大小写不匹配而传不出去都发现不了。
 * 抽成纯模块 `backendEnv.ts` 之后，这里可以真的跑它。
 */
import { describe, expect, it } from 'vitest';

import {
  BACKEND_ENV_ALLOWLIST,
  DEV_EXTRA_ENV_ALLOWLIST,
  buildRuntimeEnv,
  selectInheritedEnv,
  type BackendRuntimeOptions,
} from './backendEnv';

/** 一份混合大小写的"宿主环境"，模拟真实 Windows。 */
const HOST_ENV: NodeJS.ProcessEnv = {
  // OS 运行必需（注意拼写：Windows 是混合大小写，allowlist 里写的是全大写）
  ComSpec: 'C:/Windows/system32/cmd.exe',
  ProgramData: 'C:/ProgramData',
  ProgramFiles: 'C:/Program Files',
  'ProgramFiles(x86)': 'C:/Program Files (x86)',
  SystemRoot: 'C:/Windows',
  TEMP: 'C:/Temp',
  Path: 'C:/Windows/system32',
  // 网络与 TLS（不是凭据，必须放行）
  HTTPS_PROXY: 'http://proxy.corp:8080',
  SSL_CERT_FILE: 'C:/corp/ca.pem',
  NO_PROXY: 'localhost,127.0.0.1',
  // 凭据：一律不得继承
  OPENAI_API_KEY: 'sk-should-not-pass',
  AWS_SECRET_ACCESS_KEY: 'aws-should-not-pass',
  GOOGLE_APPLICATION_CREDENTIALS: 'C:/creds.json',
  AZURE_OPENAI_KEY: 'azure-should-not-pass',
  HF_TOKEN: 'hf-should-not-pass',
  GITHUB_TOKEN: 'gh-should-not-pass',
  DEEPSEEK_API_KEY: 'ds-should-not-pass',
  MY_PASSWORD: 'pw-should-not-pass',
  // 开发态专用
  CONDA_PREFIX: 'C:/conda',
};

const RUNTIME_OPTIONS: BackendRuntimeOptions = {
  installRoot: 'C:/install',
  dataRoot: 'C:/install/data',
  healthPort: 8081,
  wsPort: 8080,
  runtimeToken: 'r'.repeat(32),
  secretBrokerToken: 'b'.repeat(32),
  attachmentKey: 'a'.repeat(32),
  agent: 'deepseek',
  isPackaged: true,
  backendDir: 'C:/install/resources/backend',
  projectRoot: 'C:/install',
  appVersion: '1.0.36',
};

describe('backend environment construction', () => {
  it('carries the mixed-case Windows variables the interpreter needs', () => {
    const inherited = selectInheritedEnv(HOST_ENV, { isPackaged: true });

    // 这是 I6 的核心回归：旧实现精确匹配 `allowed.has(name)`，这四个**全部传不出去**。
    expect(inherited.ComSpec).toBe('C:/Windows/system32/cmd.exe');
    expect(inherited.ProgramData).toBe('C:/ProgramData');
    expect(inherited.ProgramFiles).toBe('C:/Program Files');
    expect(inherited['ProgramFiles(x86)']).toBe('C:/Program Files (x86)');
    expect(inherited.SystemRoot).toBe('C:/Windows');
    expect(inherited.Path).toBe('C:/Windows/system32');
  });

  it('keeps proxy and CA variables (not credentials, but required to reach providers)', () => {
    const inherited = selectInheritedEnv(HOST_ENV, { isPackaged: true });
    expect(inherited.HTTPS_PROXY).toBe('http://proxy.corp:8080');
    expect(inherited.SSL_CERT_FILE).toBe('C:/corp/ca.pem');
    expect(inherited.NO_PROXY).toBe('localhost,127.0.0.1');
  });

  it('drops every credential-shaped variable regardless of spelling', () => {
    const inherited = selectInheritedEnv(HOST_ENV, { isPackaged: true });
    for (const name of [
      'OPENAI_API_KEY',
      'AWS_SECRET_ACCESS_KEY',
      'GOOGLE_APPLICATION_CREDENTIALS',
      'AZURE_OPENAI_KEY',
      'HF_TOKEN',
      'GITHUB_TOKEN',
      'DEEPSEEK_API_KEY',
      'MY_PASSWORD',
    ]) {
      expect(inherited[name], `${name} 不应被继承`).toBeUndefined();
    }
  });

  it('only relaxes the allowlist for development, never for the packaged app', () => {
    const packaged = selectInheritedEnv(HOST_ENV, { isPackaged: true });
    const dev = selectInheritedEnv(HOST_ENV, { isPackaged: false });
    expect(packaged.CONDA_PREFIX).toBeUndefined();
    expect(dev.CONDA_PREFIX).toBe('C:/conda');
    // 开发放宽只涉及环境变量，不涉及凭据。
    expect(dev.OPENAI_API_KEY).toBeUndefined();
  });

  it('builds the packaged runtime variables', () => {
    const runtime = buildRuntimeEnv(RUNTIME_OPTIONS);
    expect(runtime.PYTHONIOENCODING).toBe('utf-8');
    expect(runtime.KNOWE_INSTALL_ROOT).toBe('C:/install');
    expect(runtime.KNOWE_DATA_DIR).toBe('C:/install/data');
    expect(runtime.KNOWE_WS_PORT).toBe('8080');
    expect(runtime.KNOWE_HEALTH_PORT).toBe('8081');
    expect(runtime.PLAYWRIGHT_BROWSERS_PATH).toContain('ms-playwright');
    expect(runtime.KNOWE_VERSION).toBe('1.0.36');
    // 打包态不注入 PYTHONPATH（PyInstaller 自包含）。
    expect(runtime.PYTHONPATH).toBeUndefined();
  });

  it('injects PYTHONPATH only in development', () => {
    const runtime = buildRuntimeEnv({ ...RUNTIME_OPTIONS, isPackaged: false });
    expect(runtime.PYTHONPATH).toBe('C:/install/resources/backend');
    expect(runtime.PLAYWRIGHT_BROWSERS_PATH).toBeUndefined();
  });

  it('keeps the allowlist free of credential-shaped names', () => {
    const all = [...BACKEND_ENV_ALLOWLIST, ...DEV_EXTRA_ENV_ALLOWLIST];
    expect(all.length).toBeGreaterThan(10);
    expect(
      all.some((name) => /KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL/i.test(name)),
    ).toBe(false);
  });
});
