/**
 * backendEnv.ts — 构造 spawn Python 后端时使用的环境变量。
 *
 * 为什么单独成模块：这段逻辑原先内联在 `main.ts` 里，而 `main.ts` 在模块顶层就调用
 * electron 的 `app`，导致测试**无法导入执行**——之前的测试只能从源码里抠出数组字面量
 * 做静态断言，观察不到任何真实行为（连 `ComSpec` 因大小写不匹配而传不出去都发现不了）。
 * 抽成不依赖 electron 的纯模块后，测试可以真的执行它。
 *
 * 设计要点：
 *   · **白名单**而非黑名单。旧实现复制整份 `process.env` 再按 7 类后缀删除疑似密钥，
 *     实测漏掉 `AZURE_OPENAI_KEY` / `OPENAI_API_TOKEN` / `AWS_SECRET_ACCESS_KEY` /
 *     `GOOGLE_APPLICATION_CREDENTIALS` / `HF_TOKEN` / `GITHUB_TOKEN` 等一大批名字。
 *     凭据只能经 safeStorage + 短租进入后端，不该靠宿主环境继承。
 *   · 匹配**大小写不敏感**。Windows 的变量名是混合大小写（`ComSpec`、`ProgramData`、
 *     `ProgramFiles`、`ProgramFiles(x86)`），allowlist 里写的是全大写；精确匹配会让这些
 *     变量静默传不出去。归一化只用于比较，返回时保留宿主的原始拼写。
 */

/** OS 运行必需变量（打包后必须原样传给 Python）。 */
export const BACKEND_ENV_ALLOWLIST = [
  'PATH',
  'Path',
  'PATHEXT',
  'SystemRoot',
  'SystemDrive',
  'windir',
  'COMSPEC',
  'TEMP',
  'TMP',
  'USERPROFILE',
  'HOMEDRIVE',
  'HOMEPATH',
  'APPDATA',
  'LOCALAPPDATA',
  'PROGRAMDATA',
  'PROGRAMFILES',
  'PROGRAMFILES(X86)',
  'NUMBER_OF_PROCESSORS',
  'PROCESSOR_ARCHITECTURE',
  'OS',
  'LANG',
  'LC_ALL',
  // ── 网络与 TLS ───────────────────────────────────────────────────────────
  // 第一版白名单漏掉了这些：需要走代理访问 OpenAI/Gemini 的部署、以及装了企业
  // MITM CA 的环境会**直连并失败**。它们不是凭据。后端唯一的 HTTP 客户端 httpx
  // 默认 trust_env=True，正是靠这些变量挂代理/信任 CA。
  'HTTP_PROXY',
  'HTTPS_PROXY',
  'ALL_PROXY',
  'NO_PROXY',
  'http_proxy',
  'https_proxy',
  'all_proxy',
  'no_proxy',
  'SSL_CERT_FILE',
  'SSL_CERT_DIR',
  'REQUESTS_CA_BUNDLE',
  'CURL_CA_BUNDLE',
  'NODE_EXTRA_CA_CERTS',
] as const;

/** 开发态额外放行：Conda/pyenv 环境（dev 后端用宿主解释器，需要它自己的环境）。 */
export const DEV_EXTRA_ENV_ALLOWLIST = [
  'CONDA_PREFIX',
  'CONDA_DEFAULT_ENV',
  'CONDA_PYTHON_EXE',
  'CONDA_SHLVL',
  'CONDA_EXE',
  'VIRTUAL_ENV',
] as const;

/** 强制透传的运行参数（非敏感，部署方可能显式设置）。 */
export const PASSTHROUGH_ENV = ['KNOWE_AGENT'] as const;

/** 从宿主环境挑选可继承的变量（白名单 + 大小写不敏感匹配）。 */
export function selectInheritedEnv(
  source: NodeJS.ProcessEnv,
  options: { isPackaged: boolean },
): NodeJS.ProcessEnv {
  const { isPackaged } = options;
  const allowed = new Set<string>(
    [
      ...BACKEND_ENV_ALLOWLIST,
      ...(isPackaged ? [] : DEV_EXTRA_ENV_ALLOWLIST),
      ...PASSTHROUGH_ENV,
    ].map((name) => name.toLowerCase()),
  );
  const inherited: NodeJS.ProcessEnv = {};
  for (const [name, value] of Object.entries(source)) {
    if (value !== undefined && allowed.has(name.toLowerCase())) inherited[name] = value;
  }
  // 打包后随包解释器不能继承宿主 PYTHONHOME（白名单本就不含它，这里是显式保险）。
  if (isPackaged) delete inherited.PYTHONHOME;
  return inherited;
}

/** 固定运行参数：由调用方提供具体值（便于测试传假值）。 */
export interface BackendRuntimeOptions {
  installRoot: string;
  dataRoot: string;
  healthPort: number | string;
  wsPort: number | string;
  runtimeToken: string;
  secretBrokerToken: string;
  attachmentKey: string;
  agent: string;
  isPackaged: boolean;
  backendDir: string;
  projectRoot: string;
  appVersion: string;
}

/** 与主机配置无关的那部分固定变量（可独立断言）。 */
export function buildRuntimeEnv(options: BackendRuntimeOptions): NodeJS.ProcessEnv {
  const runtime: NodeJS.ProcessEnv = {
    PYTHONUNBUFFERED: '1',
    PYTHONUTF8: '1',
    PYTHONIOENCODING: 'utf-8',
    KNOWE_INSTALL_ROOT: options.installRoot,
    KNOWE_DATA_DIR: options.dataRoot,
    KNOWE_HEALTH_PORT: String(options.healthPort),
    KNOWE_WS_PORT: String(options.wsPort),
    KNOWE_RUNTIME_TOKEN: options.runtimeToken,
    KNOWE_SECRET_BROKER_TOKEN: options.secretBrokerToken,
    KNOWE_ATTACHMENT_KEY: options.attachmentKey,
    KNOWE_AGENT: options.agent,
  };
  if (options.isPackaged) {
    runtime.PLAYWRIGHT_BROWSERS_PATH = `${options.backendDir}/ms-playwright`;
    runtime.KNOWE_BROWSER_HEADLESS = '1';
    runtime.KNOWE_VERSION = options.appVersion;
  } else {
    // 双保险：dev 后端 cwd=PROJECT_ROOT（`python -m backend` 的解析前提）；
    // PYTHONPATH 指向 backend/，让 knowe_* 顶层包可解析。
    runtime.PYTHONPATH = options.backendDir;
  }
  return runtime;
}
