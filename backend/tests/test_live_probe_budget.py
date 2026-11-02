"""The live harness cannot send an uncapped or wrong-endpoint request."""
import importlib.util
import json
from pathlib import Path
import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('probe_budget', ROOT/'scripts/budget_product_probe.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
spec2 = importlib.util.spec_from_file_location('probe_ledger', ROOT/'scripts/eval_runner.py')
runner = importlib.util.module_from_spec(spec2)
spec2.loader.exec_module(runner)


@pytest.mark.asyncio
async def test_probe_rejects_missing_cap_before_reserve_or_network(tmp_path):
    price = json.loads((ROOT/'evals/pricing.qwen3.8-max-beijing.json').read_text(encoding='utf-8'))
    ledger = runner.Ledger(tmp_path/'ledger.jsonl',50)
    guard = probe.BudgetTransport(price,ledger)
    try:
        request = httpx.Request('POST',price['endpoint']+'/chat/completions',json={'model':price['model'],'messages':[]})
        with pytest.raises(RuntimeError,match='token_bound'):
            await guard.handle_async_request(request)
        assert not guard.calls and ledger.spent()==0
    finally:
        await guard.network.aclose()
        ledger.close()


@pytest.mark.asyncio
async def test_probe_accounts_actual_sse_usage_and_rejects_foreign_host(tmp_path):
    price = json.loads((ROOT/'evals/pricing.qwen3.8-max-beijing.json').read_text(encoding='utf-8'))
    ledger = runner.Ledger(tmp_path/'ledger.jsonl',50)
    guard = probe.BudgetTransport(price,ledger)
    await guard.network.aclose()
    sends = []
    def handler(request):
        sends.append(request)
        data={'model':price['model'],'usage':{'prompt_tokens':10,'completion_tokens':3}}
        return httpx.Response(200,content=('data: '+json.dumps(data)+'\n\ndata: [DONE]\n\n').encode())
    guard.network = httpx.MockTransport(handler)
    body={'model':price['model'],'messages':[],'max_tokens':128,'stream':True}
    try:
        async with httpx.AsyncClient(transport=guard) as client:
            await client.post(price['endpoint']+'/chat/completions',json=body)
            with pytest.raises(RuntimeError,match='endpoint_not_authorized'):
                await client.post('https://example.com/chat/completions',json=body)
        assert len(sends)==1 and len(guard.calls)==1
        assert str(ledger.spent())=='0.000228'
    finally:
        await guard.network.aclose()
        ledger.close()
