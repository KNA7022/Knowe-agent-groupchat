import React from 'react';

import { useGroupStore } from '../store/groups';
import { IconAlert, IconBook, IconCheck, IconReport } from './icons';

export type ProductShellKind = 'review' | 'privacy';

export const ProductShell: React.FC<{ kind: ProductShellKind }> = ({ kind }) => {
  const groupCount = useGroupStore((state) => state.order.length);
  const sourceCount = useGroupStore((state) => Object.values(state.groups).reduce(
    (sum, group) => sum + group.source_links.length, 0,
  ));
  const pendingApprovals = useGroupStore((state) => Object.values(state.runtimeSummaries).reduce(
    (sum, summary) => sum + summary.pending_approval_count, 0,
  ));
  const review = kind === 'review';
  return <div className="product-shell">
    <header>
      <span className="product-shell-icon" aria-hidden="true">{review ? <IconReport /> : <IconAlert />}</span>
      <span className="study-eyebrow">{review ? 'Review Center' : 'Privacy Center'}</span>
      <h1>{review ? '审核中心' : '隐私中心'}</h1>
      <p>{review
        ? '集中处理真实的任务审批与后续改进候选。当前版本只展示已有运行摘要，不虚构评测或进化结果。'
        : '查看本机数据边界与当前可验证的存储范围。完整 PII 扫描、DLP 与加密金库将在后续安全里程碑接入。'}</p>
    </header>
    <section aria-label={review ? '当前审核数据' : '当前隐私数据'}>
      <h2>当前可验证数据</h2>
      <dl>
        <div><dt>本地群组</dt><dd>{groupCount}</dd></div>
        <div><dt>{review ? '待审批' : '资料引用'}</dt><dd>{review ? pendingApprovals : sourceCount}</dd></div>
      </dl>
      <div className="product-shell-notice">
        {review ? <IconCheck /> : <IconBook />}
        <div><strong>{review ? '候选不会自动生效' : '敏感资料默认不出机'}</strong>
          <p>{review
            ? '提示词、路由、检索策略和技能候选需要离线评测与人工批准。'
            : '当前页面不声称已完成 PII 检测或密文存储；安全状态将在能力真实接入后更新。'}</p></div>
      </div>
    </section>
  </div>;
};

export default ProductShell;
