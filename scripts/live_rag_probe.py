"""Synthetic scan through production RAG services. No CLI or automatic egress.

Caller supplies a shared-budget AsyncClient factory and in-memory credential.
Exactly two model requests on success; neither responses nor credentials logged.
"""
from __future__ import annotations
import base64
from io import BytesIO
import json
from pathlib import Path
import uuid

from scripts.live_protocol_probe import ROOT
from backend.commercial_gateway import ProviderGatewayClient
from backend.groups import GroupRepository
from backend.local_knowledge import LocalKnowledgeService
from backend.rag_runtime import RagRuntime
from backend.task_runtime.service import TaskRuntimeService


def synthetic_scan_pdf():
    """One image-only 320x220pt PDF, actual local renderer produces640x440 PNG."""
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.pdfgen.canvas import Canvas
    from reportlab.lib.utils import ImageReader
    image = Image.new("RGB", (640, 440), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("arial.ttf", 28)
    except OSError:
        font = ImageFont.load_default()
    for index, text in enumerate(("SYNTHETIC STUDY MINUTES", "Course A: 12 minutes", "Course B: 27 minutes", "Total: 39 minutes")):
        draw.text((35, 35 + index * 70), text, fill="black", font=font)
    png = BytesIO(); image.save(png, format="PNG")
    pdf = BytesIO(); canvas = Canvas(pdf, pagesize=(320, 220))
    canvas.drawImage(ImageReader(BytesIO(png.getvalue())), 0, 0, width=320, height=220)
    canvas.showPage(); canvas.save()
    return pdf.getvalue()


async def run_rag_probe(data_root, binding, credential, client_factory):
    """Real import/render/approve/vision/index/approve/answer/exact-citation chain.

Synthetic only. Dedicated child directory preserves caller's existing app state.
Successful answers remain PARTIAL because semantic entailment is not automated.
Caller must enforce image pixel limits and shared paid budget on every send.
"""
    if not all(binding.get(k) for k in ("provider", "model", "base_url")) or not credential or not callable(client_factory):
        raise ValueError("explicit binding, credential and budget client factory required")
    root = Path(data_root) / ("rag-probe-" + uuid.uuid4().hex)
    groups = GroupRepository(root)
    group = groups.create_group("Synthetic scan RAG probe")
    knowledge = LocalKnowledgeService(root, groups)
    runtime = TaskRuntimeService(root, groups)
    def factory(authority, _group):
        return ProviderGatewayClient(base_url=binding["base_url"], model=binding["model"],
            provider=binding["provider"], transport=binding.get("transport", "openai_chat"),
            api_key="", credential_provider=lambda: credential, authority_provider=lambda: authority,
            max_retries=0, client_factory=client_factory)
    rag = RagRuntime(knowledge, runtime, factory)
    knowledge.rag_runtime = rag
    knowledge.vision_reader, knowledge.answer_reader = rag.vision, rag.answer
    report = {"pass": False, "scope": "synthetic real LocalKnowledgeService/RagRuntime chain, not desktop UI",
              "render_pixels": [640, 440], "maximum_model_requests": 2, "output_cap": 2048,
              "stages": [], "data_root": str(root)}
    async def dispatch(route, payload):
        response = await knowledge.dispatch("POST", "/api/knowledge/local/" + route,
            "group_id=" + group.group_id, json.dumps(payload).encode("utf-8"))
        report["stages"].append({"route": route, "status": response.status})
        return response.status, response.body
    async def approved_call(route, payload):
        status, pending = await dispatch(route, payload)
        if status != 202:
            raise ValueError("expected_actual_rag_pending_approval")
        runtime.resolve_approval(group.group_id, pending["approval_id"], approved=True,
            capabilities=("provider_egress",), request_key="synthetic-approve:" + pending["operation_id"])
        status, result = await dispatch(route, {**payload, "authorization": {"operation_id": pending["operation_id"]}})
        if status != 200:
            raise ValueError("rag_stage_failed_" + str(status))
        return result, pending
    try:
        status, imported = await dispatch("import", {"filename": "synthetic-study-scan.pdf",
            "data_base64": base64.b64encode(synthetic_scan_pdf()).decode("ascii")})
        if status != 200 or imported["source"]["status"] != "needs_vision":
            raise ValueError("scan_import_did_not_require_vision")
        source = imported["source"]
        vision, vision_pending = await approved_call("vision", {"source_id": source["source_id"],
            "source_version": source["version"], "page_limit": 1, "approved": True})
        report["vision_run_status"] = runtime.store.get_run(vision_pending["run_id"]).status.value
        report["vision_source_version"] = vision["source"]["version"]
        report["vision_remaining_pages"] = vision["remaining_pages"]
        status, indexed = await dispatch("search", {"query": "minutes"})
        if status != 200 or not indexed["results"]:
            raise ValueError("vision_text_not_in_actual_index")
        answer, pending = await approved_call("answer", {"query": "How many total study minutes are listed?",
            "approved": True, "use_model": True})
        report.update(verification=answer["verification"], citation_count=len(answer["citations"]),
            answer_run_status=runtime.store.get_run(pending["run_id"]).status.value,
            semantic_entailment_verified=answer.get("semantic_entailment_verified", False))
        report["pass"] = answer["verification"] == "exact_quotes_verified" and bool(answer["citations"]) and "39" in answer["answer"]
        if not report["pass"]:
            report["error"] = "citation_or_synthetic_total_verification_failed"
    except Exception as exc:
        report["error"] = type(exc).__name__
    finally:
        runtime.close()
        knowledge.close()
    return report
