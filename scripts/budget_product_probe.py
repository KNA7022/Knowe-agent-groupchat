"""Budget guard around real HTTP for the synthetic protected product probe.

No mocked response, request mutation, credential persistence or implicit retries.
The HTTP response is buffered in memory to account before handing it to the
production SSE parser. Thus this probe verifies SSE parsing, not token latency.
"""
from __future__ import annotations
import asyncio
import json
import os
import traceback
import base64
import io
from decimal import Decimal
from pathlib import Path
import httpx


class BudgetTransport(httpx.AsyncBaseTransport):
    def __init__(self, pricing, ledger, native_protocols=False, synthetic_vision=False):
        self.pricing, self.ledger = pricing, ledger
        self.network = httpx.AsyncHTTPTransport(retries=0)
        self.calls = []
        self.rejections = []
        self.failed = False
        self.synthetic_vision = synthetic_vision
        base = pricing["endpoint"].rstrip("/")
        self.endpoints = {base+"/chat/completions"}
        if native_protocols:
            self.endpoints.update({base+"/responses", base.removesuffix("/compatible-mode/v1")+"/apps/anthropic/v1/messages"})

    async def handle_async_request(self, request):
        price = self.pricing
        if self.failed or len(self.calls) >= 3:
            self.rejections.append("call_limit_or_prior_uncertainty")
            raise RuntimeError("probe_call_limit_or_prior_uncertainty")
        approved = any(request.url.scheme == "https" and request.url.host == httpx.URL(url).host
                       and request.url.path == httpx.URL(url).path and request.url.port in {None,443}
                       and not request.url.query and not request.url.userinfo for url in self.endpoints)
        if request.method != "POST" or not approved:
            self.rejections.append(dict(reason="endpoint_not_authorized",method=request.method,host=request.url.host,path=request.url.path))
            raise RuntimeError("probe_endpoint_not_authorized")
        wire = json.loads(request.content)
        cap = wire.get("max_tokens", wire.get("max_completion_tokens", wire.get("max_output_tokens")))
        images = []
        def bounded(value):
            if isinstance(value,dict): return {key:bounded(item) for key,item in value.items()}
            if isinstance(value,list): return [bounded(item) for item in value]
            if isinstance(value,str) and value.startswith('data:image/'):
                if not self.synthetic_vision or not value.startswith('data:image/png;base64,'):
                    raise RuntimeError('probe_image_not_authorized')
                data=base64.b64decode(value.split(',',1)[1],validate=True)
                from PIL import Image
                image=Image.open(io.BytesIO(data))
                if len(data)>1000000 or image.size!=(640,440) or image.format!='PNG':
                    raise RuntimeError('probe_image_budget_not_proven')
                images.append(8192)
                return 'bounded_synthetic_640x440_png'
            return value
        text_wire = bounded(wire)
        if len(images)>1:
            raise RuntimeError('probe_image_count_exceeded')
        input_cap = len(json.dumps(text_wire,ensure_ascii=False).encode()) + 2048 + sum(images)
        if wire.get("model") != price["model"] or type(cap) is not int or not 1 <= cap <= 4096 or input_cap > 50000:
            self.rejections.append(dict(reason="model_or_bound",model_matches=wire.get("model")==price["model"],cap=cap,input_cap=input_cap))
            raise RuntimeError("probe_model_or_token_bound_rejected_before_send")
        # No images/remote content in this source-text probe. Bounding complete
        # UTF-8 JSON bytes also bounds the strings and tool schemas it contains.
        if (b"image_url" in request.content and not images) or b"input_audio" in request.content:
            raise RuntimeError("probe_only_synthetic_text")
        unit_in = Decimal(str(price["input_cny_per_million"])) / 1000000
        unit_out = Decimal(str(price["output_cny_per_million"])) / 1000000
        reserved = input_cap*unit_in + cap*unit_out
        ticket = self.ledger.reserve("protected-product-"+str(len(self.calls)+1), reserved)
        row = dict(ticket=ticket, input_cap=input_cap, output_cap=cap, status="reserved")
        self.calls.append(row)
        try:
            response = await self.network.handle_async_request(request)
            content = await response.aread()
            await response.aclose()
            if response.status_code != 200 or len(content) > 4000000:
                raise RuntimeError("probe_http_or_response_bound_failure")
            packets = []
            if wire.get("stream"):
                for line in content.decode("utf-8").splitlines():
                    if line.startswith("data:") and line[5:].strip() != "[DONE]":
                        packets.append(json.loads(line[5:]))
            else:
                packets.append(json.loads(content))
            envelopes = [obj for packet in packets for obj in (packet,packet.get("message"),packet.get("response")) if isinstance(obj,dict)]
            usages = [p["usage"] for p in envelopes if isinstance(p.get("usage"),dict)]
            models = {p["model"] for p in envelopes if p.get("model")}
            if not models or not models.issubset({price["model"], *price.get("allowed_response_model_ids", [])}):
                raise RuntimeError("probe_unverified_response_model")
            ins = [u.get("prompt_tokens",u.get("input_tokens")) for u in usages]
            outs = [u.get("completion_tokens",u.get("output_tokens")) for u in usages]
            inp = max((v for v in ins if type(v) is int),default=None)
            out = max((v for v in outs if type(v) is int),default=None)
            if inp is not None:
                inp += max((u.get("cache_creation_input_tokens",0)+u.get("cache_read_input_tokens",0) for u in usages),default=0)
            if type(inp) is not int or type(out) is not int or not 0 <= inp <= input_cap or not 0 <= out <= cap:
                raise RuntimeError("probe_usage_unknown_or_outside_bound")
            actual = inp*unit_in + out*unit_out
            self.ledger.settle(ticket, reserved, actual)
            row.update(status="accounted", usage=dict(input_tokens=inp, output_tokens=out), cny=str(actual))
            decoded_headers = {k:v for k,v in response.headers.items() if k.lower() not in {'content-encoding','content-length'}}
            return httpx.Response(response.status_code, headers=decoded_headers,
                                  content=content, extensions=response.extensions)
        except BaseException:
            self.failed = True
            row["status"] = ('accounted_response_delivery_failed' if row['status']=='accounted'
                             else "uncertain_full_reservation_retained")
            # Leave the outstanding reservation unresolved and block more paid
            # work until an explicit audit; never refund an unknown response.
            raise

    async def aclose(self):
        # The shared transport is explicitly closed after all engine clients.
        pass


def main(args, ledger_class):
    if not args.pricing or not args.ledger:
        raise ValueError("verified_pricing_and_shared_ledger_required")
    price = json.loads(Path(args.pricing).read_text(encoding="utf-8"))
    if price.get("verified") is not True or price.get("currency") != "CNY" or price.get("transport") != "openai_chat":
        raise ValueError("unverified_probe_pricing")
    for name in ("input_cny_per_million", "output_cny_per_million"):
        if not Decimal(str(price[name])).is_finite() or Decimal(str(price[name])) <= 0:
            raise ValueError("invalid_probe_price")
    credential = os.environ.get(price.get("credential_env", "STUDYCREW_EVAL_API_KEY"))
    if not credential:
        raise ValueError("credential_unavailable")
    ledger = ledger_class(args.ledger, price["budget_cny"])
    transport = BudgetTransport(price, ledger, native_protocols=args.mode=="protocol-probe",synthetic_vision=args.mode=="rag-probe")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"scope": args.mode+"_real_provider_http_not_desktop", "passed": False}
    async def run():
        from live_product_probe import run_product_probe
        binding = {"provider":price["provider"], "model":price["model"], "base_url":price["endpoint"], "transport":price["transport"]}
        try:
            factory = lambda: httpx.AsyncClient(transport=transport, timeout=120, follow_redirects=False)
            if args.mode=="protocol-probe":
                from live_protocol_probe import run_protocol_probe
                results = await run_protocol_probe(binding,credential,factory)
                return {"ok":all(r["pass"] for r in results),"protocols":results}
            if args.mode in {"rag-probe","ui-probe","memory-probe"}:
                from importlib import import_module
                selected = getattr(import_module('live_'+args.mode.replace('-','_')), 'run_'+args.mode.replace('-','_'))
                return await selected(output.parent/(output.stem+'-data'),binding,credential,factory)
            return await run_product_probe(output.parent/(output.stem+"-data"), binding=binding,
                 credential=credential, client_factory=factory)
        finally:
            await transport.network.aclose()
    try:
        report["result"] = asyncio.run(run())
        report["passed"] = bool(report["result"].get("ok", report["result"].get("pass", False)))
    except Exception as exc:
        # Exception messages can include request headers; type alone is safe.
        report["error_type"] = type(exc).__name__
        report["error_frames"] = [{"file":Path(f.filename).name,"line":f.lineno,"function":f.name}
                                  for f in traceback.extract_tb(exc.__traceback__)]
    finally:
        report.update(calls=transport.calls, rejections=transport.rejections, task_total_cny_upper_bound=str(ledger.spent()))
        ledger.close()
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k!='result'}, ensure_ascii=False))
