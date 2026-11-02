"""Reconcile retained evaluation evidence without any network or credential access."""
from collections import Counter
from decimal import Decimal
import json
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1] / 'evals' / 'runs'
    ledger = [json.loads(line) for line in (root / 'paid-ledger.jsonl').read_text(encoding='utf-8').splitlines() if line]
    tickets = {}
    for row in ledger:
        tickets[row['ticket']] = tickets.get(row['ticket'], Decimal(0)) + Decimal(row['cny'])
    cases = [json.loads(line) for line in (root / 'live-qwen.jsonl').read_text(encoding='utf-8').splitlines() if line]
    usage = Counter()
    for row in cases:
        usage.update(row.get('usage') or {})
    probes = {}
    calls = {}
    for file in sorted(root.glob('*live*.json')):
        data = json.loads(file.read_text(encoding='utf-8'))
        if 'calls' not in data:
            continue
        probes[file.name] = {'passed': data.get('passed'), 'calls': len(data['calls'])}
        for row in data['calls']:
            if row['ticket'] in calls:
                raise ValueError('duplicate_probe_ticket')
            calls[row['ticket']] = row
            usage.update(row.get('usage') or {})
    known_cost = sum((Decimal(row.get('cny', '0')) for row in cases if row.get('usage')), Decimal(0))
    known_cost += sum((tickets[row['ticket']] for row in calls.values() if row.get('usage')), Decimal(0))
    upper = sum(tickets.values(), Decimal(0))
    result = {
        'currency': 'CNY', 'budget': '50', 'known_usage_tokens': dict(usage),
        'known_usage_cost_at_verified_uncached_rates': str(known_cost),
        'unknown_usage_retained_upper_bound': str(upper - known_cost),
        'task_cost_upper_bound': str(upper), 'remaining_budget_lower_bound': str(Decimal(50) - upper),
        'dataset_attempts': len(cases), 'probe_paid_requests': len(calls),
        'probes': probes,
        'basis': 'Verified published rates and provider usage, not an account invoice. Unknown usage retains the full reservation.',
        'historical_report_correction': 'rag-live.json response delivery failed after usage settlement; its known cost is included once, despite the old uncertain status label.',
    }
    if upper > Decimal(50) or upper < known_cost:
        raise ValueError('ledger_reconciliation_failed')
    (root / 'cost-final.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    annotations = {row['id']: row for row in (json.loads(line) for line in (root.parent / 'v1.jsonl').read_text(encoding='utf-8').splitlines() if line)}
    latest = {row['id']: row for row in cases}
    failures = [row for row in latest.values() if row['status'] != 'passed']
    lines = ['# 固定 v1 真实模型逐例失败报告', '',
             '按每个场景最后一次实际结果列出，旧尝试仍保留于 live-qwen.jsonl。未改变预期或放宽比较；这是模型行为评测，不是桌面端到端验收。', '',
             f'场景数 {len(latest)}；未通过 {len(failures)}。费用总计另见 cost-final.json。', '']
    for row in failures:
        expected = {item['check']: item['equals'] for item in annotations[row['id']]['expected']}
        actual = row.get('evidence', {}).get('actual')
        failed_checks = [name for name, passed in row.get('checks', {}).items() if not passed]
        lines += [f"## {row['id']} · {row['split']}", '',
                  '失败字段：' + ', '.join(failed_checks), '',
                  '固定预期：', '```json', json.dumps(expected, ensure_ascii=False, indent=2), '```',
                  '实际输出：', '```json', json.dumps(actual, ensure_ascii=False, indent=2), '```', '']
    (root / 'failures.md').write_text('\n'.join(lines), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
