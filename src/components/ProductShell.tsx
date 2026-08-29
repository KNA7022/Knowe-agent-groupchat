import React from 'react';

import { useGroupStore } from '../store/groups';
import { useTaskRuntimeStore } from '../store/taskRuntime';
import { IconAlert, IconBook, IconCheck, IconReport } from './icons';

export type ProductShellKind = 'review' | 'privacy';

export const ProductShell: React.FC<{ kind: ProductShellKind }> = ({ kind }) => {
  const groupCount = useGroupStore((state) => state.order.length);
  const sourceCount = useGroupStore((state) => Object.values(state.groups).reduce(
    (sum, group) => sum + group.source_links.length, 0,
  ));
  const pendingApprovals = useTaskRuntimeStore((state) => Object.values(state.groupSummaries).reduce(
    (sum, summary) => sum + summary.pending_approval_count, 0,
  ));
  const review = kind === 'review';
  return <div className="product-shell">
    <header>
      <span className="product-shell-icon" aria-hidden="true">{review ? <IconReport /> : <IconAlert />}</span>
      <span className="study-eyebrow">{review ? 'Review Center' : 'Privacy Center'}</span>
      <h1>{review ? '审核中心' : '隐私中心'}</h1>
      <p>{review
        ? '集中处理真实的任务审批与后续改进候选。工具、外发与权限扩大复用同一套任务审批记录。'
        : '查看本机数据边界与当前可验证的安全状态：系统加密凭据、短期租约与敏感信息出站审批。'}</p>
    </header>
    <section aria-label={review ? '当前审核数据' : '当前隐私数据'}>
      <h2>当前可验证数据</h2>
      <dl>
        <div><dt>本地群组</dt><dd>{groupCount}</dd></div>
        <div><dt>{review ? '待审批' : '资料引用'}</dt><dd>{review ? pendingApprovals : sourceCount}</dd></div>
        {!review && <>
          <div><dt>凭据存储</dt><dd>系统加密</dd></div>
          <div><dt>敏感出站</dt><dd>逐次审批</dd></div>
        </>}
      </dl>
      <div className="product-shell-notice">
        {review ? <IconCheck /> : <IconBook />}
        <div><strong>{review ? '候选不会自动生效' : '敏感资料默认不出机'}</strong>
          <p>{review
            ? '高风险工具授权绑定群组、任务、工具与精确作用域；授权不可跨工具或重复使用。'
            : '提供方密钥只在 Electron 主进程持久化；后端仅接收用途绑定、最长五分钟的内存租约。PII 或学生敏感资料外发前必须得到新批准。'}</p></div>
      </div>
    </section>
  </div>;
};

export default ProductShell;
