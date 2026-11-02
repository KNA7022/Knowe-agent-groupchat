# StudyCrew 可复现评测

本次最终总费用与 token 统计见 [cost-final.json](runs/cost-final.json)，可运行 `python scripts/eval_cost_report.py` 从保留账本及逐次报告重新汇总。`live-qwen.aggregate.json` 的历史费用仅对应当时数据集阶段，不能替代包含产品验证调用的最终总计。

`v1.jsonl` 是固定的 120 个合成场景：资料问答 24、实际图片 24、计划排程 24、多协议 16、权限决策 16、记忆治理 16。`v1.manifest.json` 固定 SHA-256，图片另有逐文件 SHA-256。开发集 90，留出集 30；留出资料主题与图片数据独立，权限/协议/记忆仍共享规则族，不能宣称覆盖独立真实用户分布。任何修改标注都应发布新版本，保留旧版本结果。

## 三种证据

1. `--mode offline`：执行实际本地排程、协议编码、记忆分类和检索组件。未生成答案的检索只记录 Recall@10，并将完整问答判为 blocked；图片与权限模型行为同样不能用 Mock 代替通过。
2. `backend/tests/test_provider_protocols.py` 等：实际 HTTP 客户端通过 MockTransport 检验三种协议、流式工具参数、错误与用量。属于模拟接口验证。
3. `--mode live`：真实提供方请求，使用产品协议转换器；回答与固定 JSON 预期做严格比较。图片题的答案不出现在文本提示中。权限和协议场景衡量模型理解规则，**不能证明应用权限拦截或三个线上协议均已调用**。桌面审批/Worker/工具/恢复另有产品验证。

严格字面匹配会把语义相近答案判失败，例如“乘积”与“各自概率的乘积”。原始逐例结果保留，不能修改答案或放宽断言后冒充原评测通过。

## 命令（Python 3.12）

```powershell
py -3.12 scripts/eval_runner.py --mode validate
py -3.12 scripts/eval_runner.py --mode offline --split development --output evals/runs/local-development.jsonl
py -3.12 scripts/eval_session.py
```

最后一条通过隐藏输入读取临时凭据，只存在进程内存中，不写入文件、命令行参数或报告。输入凭据后，输入一行参数 JSON，例如：

```json
["--mode","live","--pricing","evals/pricing.qwen3.8-max-beijing.json","--ledger","evals/runs/paid-ledger.jsonl","--output","evals/runs/live-qwen.jsonl","--ids","knowledge-001,multimodal-001,permissions-002","--rpm","12"]
```

先检查试跑结果，再改为 `--split development`，最后 `--split holdout`。输入 `[]` 结束凭据会话。不得把 API Key 放入命令行。正式演示使用桌面凭据保险库；这个会话只是限额评测工具。

## 费用与恢复

所有本次真实调用共用 `paid-ledger.jsonl`，上限人民币 50 元。每次网络请求前先持久化最坏输入/输出预留；返回合法用量后按未命中缓存原价结算，优惠和免费额度不抵扣预算。执行时不充值、不更换模型、无隐藏 SDK 重试。

429 保留整笔预留、受限退避；网络状态不明或用量不合法立即停止。运行锁防止两个进程同时花费，未结算请求阻止恢复。不能删除账本或更换账本绕过额度。未知费用只能核对账单，或在能证明单次上界的情况下永久按全部预留记账，注明“上界而非实付”。2026-10-11 有一笔旧适配器 JSON 解析异常按全额预留入账，原记录保留。

`*.jsonl` 逐次落盘，`*.summary.json` 包含逐例失败/blocked。恢复时跳过相同运行标识下通过的案例；实现、配置或数据变化产生新标识，不能混为同一版本。不同版本结果汇总必须保留每次 `run_identity`。

本仓库中的模型价目是经用户确认 endpoint 后的北京地域原价快照，不包含凭据。费率变更后须重新核实官方页面。首次真实调用 3 项成功的已知原价费用为 0.008352 元；最新总量以账本和最终报告为准。
