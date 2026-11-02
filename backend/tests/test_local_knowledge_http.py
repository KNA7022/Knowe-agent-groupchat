from __future__ import annotations

import asyncio
import base64
from io import BytesIO
import json
import sqlite3
import threading
from urllib.parse import urlencode
import zipfile

import pypdfium2 as pdfium
import pytest

from backend.config import CONFIG
from backend import local_knowledge
from backend.knowe_rag.crypto import MasterKey
from backend.knowe_rag.parsers import parse_document_bytes, UnsupportedFormat
from backend.server import KnoweServer


@pytest.fixture
async def app(tmp_path):
    server = KnoweServer(data_dir=str(tmp_path / "backend"))
    listener = await asyncio.start_server(server._health_conn, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    group = server.groups.create_group("Local group")
    other = server.groups.create_group("Other group")
    server.groups.bind_course(group.group_id, "calculus")

    async def http(route, payload=None, *, group_id=group.group_id, course=None, token=CONFIG.runtime_token, extra=None):
        params = {"group_id": group_id, **({"course_id": course} if course else {}), **(extra or {})}
        path = route if route.startswith("/") else f"/api/knowledge/local/{route}?{urlencode(params)}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b""
        method = "POST" if payload is not None else "GET"
        headers = f"Host: 127.0.0.1\r\nX-Knowe-Runtime-Token: {token}\r\n"
        if payload is not None:
            headers += f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"{method} {path} HTTP/1.1\r\n{headers}\r\n".encode("ascii") + body)
        await writer.drain()
        response = await asyncio.wait_for(reader.read(), 15)
        writer.close()
        await writer.wait_closed()
        head, raw = response.split(b"\r\n\r\n", 1)
        return int(head.split()[1]), json.loads(raw)

    yield server, group, other, http
    listener.close()
    await listener.wait_closed()
    await server._stop_task_runtime_group_deletion_reconcile()
    if server.local_knowledge:
        server.local_knowledge.close()
    if server.store:
        server.store.close(timeout=10)
    server.task_runtime.close()
    server.secret_leases.close()


def document(text="二次函数与导数是微积分复习证据。", filename="私密讲义.txt"):
    return {"filename": filename, "data_base64": base64.b64encode(text.encode("utf-8")).decode("ascii")}


async def test_real_listener_import_search_inspect_restart_delete_and_ciphertext(app):
    server, group, other, http = app
    assert (await http("sources", token="wrong"))[0] == 401
    status, imported = await http("import", document(), course="calculus")
    assert status == 200 and imported["mode"] == "lexical" and imported["degraded"]
    source = imported["source"]
    source_id = source["source_id"]
    assert source_id.startswith("src_") and source["status"] == "indexed"
    assert [link["source_id"] for link in imported["group"]["source_links"]] == [source_id]
    assert imported["group"]["group_id"] == group.group_id
    assert (await http("sources", course="calculus"))[1]["sources"] == [source]
    assert (await http("sources", group_id=other.group_id))[1]["sources"] == []
    status, result = await http("search", {"query": "导数"}, course="calculus")
    assert status == 200 and len(result["results"]) == 1
    hit = result["results"][0]
    assert hit["source_id"] == source_id and hit["location"] == "para:1#1"
    detail = (await http("detail", course="calculus", extra={"source_id": source_id, "chunk_id": hit["chunk_id"]}))[1]
    assert detail["chunks"][0]["text"] == hit["text"]
    assert server.local_knowledge.store.read_original(source_id) == "二次函数与导数是微积分复习证据。".encode("utf-8")
    db_path = server.local_knowledge.store.path
    server.local_knowledge.store.checkpoint()
    for file in db_path.parent.iterdir():
        raw = file.read_bytes()
        assert "私密讲义".encode("utf-8") not in raw and "二次函数".encode("utf-8") not in raw
    with sqlite3.connect(db_path) as db:
        manifest = db.execute("SELECT metadata, original FROM rag_sources").fetchone()
        assert all(isinstance(value, bytes) for value in manifest)
    # Reopen through production lazy dispatch; the scoped BM25 corpus is rebuilt from ciphertext.
    server.local_knowledge.close()
    server.local_knowledge = None
    assert (await http("search", {"query": "导数"}, course="calculus"))[1]["results"] == result["results"]
    deleted_status, deleted = await http("delete", {"source_id": source_id}, course="calculus")
    assert deleted_status == 200 and deleted["group"]["source_links"] == []
    assert (await http("search", {"query": "导数"}))[1]["results"] == []
    with sqlite3.connect(db_path) as db:
        for table in ("rag_documents", "rag_chunks", "rag_sources", "rag_embeddings"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    server.local_knowledge.close()
    server.local_knowledge = None
    assert (await http("sources"))[1]["sources"] == []


async def test_scope_course_archived_pending_and_deleted_groups(app):
    server, group, other, http = app
    source_id = (await http("import", document(), course="calculus"))[1]["source"]["source_id"]
    for route, payload in (("sources", None), ("detail", None), ("search", {"query": "导数"}), ("delete", {"source_id": source_id})):
        assert (await http(route, payload, course="unbound", extra={"source_id": source_id}))[0] == 403
        assert (await http(route, payload, group_id="grp_" + "f" * 32, extra={"source_id": source_id}))[0] == 403
    assert (await http("detail", group_id=other.group_id, extra={"source_id": source_id}))[0] == 403
    assert (await http("delete", {"source_id": source_id}, group_id=other.group_id))[0] == 403
    server.groups.archive(group.group_id)
    assert (await http("search", {"query": "导数"}))[0] == 200
    assert (await http("import", document()))[0] == 403
    assert (await http("delete", {"source_id": source_id}))[0] == 403
    server.groups.restore(group.group_id)
    preview = server.groups.delete_preview(group.group_id)
    tx = server.groups.prepare_delete(group.group_id, preview.confirmation_token)
    assert (await http("search", {"query": "导数"}))[0] == 403
    server.groups.mark_runtime_deleted(tx["transaction_id"])
    server.groups.commit_delete(tx["transaction_id"])
    assert (await http("sources"))[0] == 403


async def test_group_delete_dispatch_retains_shared_then_removes_last_source(app):
    server, group, other, http = app
    source_id = (await http("import", document()))[1]["source"]["source_id"]
    server.groups.link_source(other.group_id, source_id)
    server.local_knowledge.store.store_embedding(server.local_knowledge.store.read_chunks(source_id)[-1].content_hash, [1.0], model="fixture")
    for target in (group, other):
        preview = server.groups.delete_preview(target.group_id)
        status, body = await http("/groups/delete", {"group_id": target.group_id, "confirmation_token": preview.confirmation_token})
        assert status == 200 and body["deleted"]
        if target is group:
            assert (await http("search", {"query": "导数"}, group_id=other.group_id))[1]["results"]
            assert server.local_knowledge.store.read_original(source_id)
        else:
            assert server.local_knowledge.store.read_original(source_id) is None
            assert server.local_knowledge.store.document_ids() == ()
            assert server.local_knowledge.store.embedding_hashes(model="fixture") == ()
            assert not server.local_knowledge._cache


async def test_wrong_key_fails_dispatch_without_overwrite(app, monkeypatch):
    server, _, _, http = app
    source_id = (await http("import", document()))[1]["source"]["source_id"]
    correct_key = server.local_knowledge.store._key
    server.local_knowledge.close()
    server.local_knowledge = None
    monkeypatch.setattr(MasterKey, "load_or_create", lambda _: MasterKey(b"z" * 32))
    assert (await http("sources"))[0] == 503
    assert (await http("import", document("replacement")))[0] == 503
    monkeypatch.setattr(MasterKey, "load_or_create", lambda _: correct_key)
    assert (await http("detail", extra={"source_id": source_id}))[1]["chunks"][0]["text"].startswith("二次函数")


@pytest.mark.parametrize("change", ["delete", "archive", "unbind"])
async def test_delayed_import_revalidates_and_never_publishes_after_scope_change(app, monkeypatch, change):
    server, group, _, http = app
    entered, release = threading.Event(), threading.Event()
    real_parse = local_knowledge.parse_document_bytes

    def delayed(*args):
        entered.set()
        assert release.wait(5)
        return real_parse(*args)

    monkeypatch.setattr(local_knowledge, "parse_document_bytes", delayed)
    pending = asyncio.create_task(http("import", document(), course="calculus"))
    await asyncio.to_thread(entered.wait, 5)
    if change == "delete":
        preview = server.groups.delete_preview(group.group_id)
        server.groups.confirmed_delete(group.group_id, preview.confirmation_token)
    elif change == "archive":
        server.groups.archive(group.group_id)
    else:
        server.groups.unbind_course(group.group_id, "calculus")
    release.set()
    assert (await pending)[0] == 403
    assert server.local_knowledge.store.document_ids() == ()


@pytest.mark.parametrize("change", ["delete_source", "unbind"])
async def test_delayed_search_rechecks_source_and_course_before_response(app, monkeypatch, change):
    server, group, _, http = app
    source_id = (await http("import", document(), course="calculus"))[1]["source"]["source_id"]
    index, _ = server.local_knowledge.corpus(group.group_id, "calculus")
    entered, release = threading.Event(), threading.Event()
    real_search = index.search

    def delayed(*args, **kwargs):
        result = real_search(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(index, "search", delayed)
    pending = asyncio.create_task(http("search", {"query": "导数"}, course="calculus"))
    await asyncio.to_thread(entered.wait, 5)
    if change == "delete_source":
        assert (await http("delete", {"source_id": source_id}))[0] == 200
    else:
        server.groups.unbind_course(group.group_id, "calculus")
    release.set()
    status, body = await pending
    if change == "delete_source":
        assert status == 200 and body["results"] == []
    else:
        assert status == 403


async def test_scan_pdf_is_needs_vision_and_bad_imports_never_read_paths(app):
    server, _, _, http = app
    pdf = pdfium.PdfDocument.new()
    pdf.new_page(100, 100)
    buffer = BytesIO()
    pdf.save(buffer)
    pdf.close()
    status, body = await http("import", {"filename": "scan.pdf", "data_base64": base64.b64encode(buffer.getvalue()).decode("ascii")})
    assert status == 200 and body["source"]["status"] == "needs_vision" and body["source"]["chunk_count"] == 0
    assert (await http("search", {"query": "scan"}))[1]["results"] == []
    for payload in ({"path": "C:/private.txt"}, document(filename="../../private.txt"), document(filename="legacy.doc"), {"filename": "x.txt", "data_base64": "not base64"}):
        assert (await http("import", payload))[0] == 400


def test_in_memory_ooxml_decompression_limit():
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"x" * (16 * 1024 * 1024 + 1))
    with pytest.raises(UnsupportedFormat):
        parse_document_bytes(buffer.getvalue(), ".docx")


@pytest.mark.parametrize("suffix", [".txt", ".md", ".html", ".docx", ".pptx", ".xlsx", ".pdf"])
async def test_real_import_route_supports_seven_native_formats(app, tmp_path, suffix):
    from backend.tests.test_rag_office_parsers import _docx, _p, _pptx, _slide, _xlsx, _row, _inline_cell
    from backend.tests.test_rag_pdf_parser import _build_pdf, _text_stream
    if suffix == ".docx":
        raw = _docx(tmp_path / "fixture.docx", _p("Unique evidence")).read_bytes()
    elif suffix == ".pptx":
        raw = _pptx(tmp_path / "fixture.pptx", {1: _slide("Unique evidence")}).read_bytes()
    elif suffix == ".xlsx":
        raw = _xlsx(tmp_path / "fixture.xlsx", {"Private sheet": _row(1, _inline_cell("A1", "Unique evidence"))}).read_bytes()
    elif suffix == ".pdf":
        raw = _build_pdf([_text_stream(["Unique evidence"])])
    else:
        raw = ("<p>Unique evidence</p>" if suffix == ".html" else "Unique evidence").encode("utf-8")
    server, _, _, http = app
    status, imported = await http("import", {"filename": "fixture" + suffix, "data_base64": base64.b64encode(raw).decode("ascii")})
    assert status == 200 and imported["source"]["status"] == "indexed"
    status, body = await http("search", {"query": "evidence"})
    assert status == 200 and len(body["results"]) == 1 and "Unique evidence" in body["results"][0]["text"]
    assert server.local_knowledge.store.read_original(imported["source"]["source_id"]) == raw


async def test_local_delete_retains_shared_source_and_group_unlink_cleans_after_restart(app):
    server, group, other, http = app
    source_id = (await http("import", document()))[1]["source"]["source_id"]
    server.groups.link_source(other.group_id, source_id)
    status, body = await http("delete", {"source_id": source_id})
    assert status == 200 and body["retained_for_other_groups"]
    assert (await http("sources"))[1]["sources"] == []
    assert (await http("search", {"query": "导数"}, group_id=other.group_id))[1]["results"]
    server.local_knowledge.close()
    server.local_knowledge = None
    status, body = await http("/groups/source-unlink", {"group_id": other.group_id, "source_id": source_id})
    assert status == 200 and body["reference_count"] == 0
    assert server.local_knowledge.store.document_ids() == ()


async def test_restart_cleans_interrupted_import_before_group_link(app):
    from backend.knowe_rag.chunks import build_chunks
    server, _, _, http = app
    await http("sources")
    source_id = "src_" + "e" * 32
    server.local_knowledge.store.write_document(source_id, build_chunks(source_id, parse_document_bytes(b"orphan evidence", ".txt")))
    server.local_knowledge.close()
    server.local_knowledge = None
    assert (await http("sources"))[1]["sources"] == []
    assert source_id not in server.local_knowledge.store.document_ids()


async def test_local_import_keeps_real_legacy_group_assets_and_course_fence(app):
    from backend.knowledge_api import register_project, unregister_project
    from backend.knowledge_assets import KnowledgeAssetManager
    from backend.knowledge_graph import KnowledgeGraphManager
    server, _, _, http = app
    project_id = "legacy-local-regression"
    group = server.groups.create_group("Legacy knowledge", legacy_project_id=project_id)
    server.groups.bind_course(group.group_id, "calculus")
    workspace = server.data_root / "legacy-fixture"
    graph_manager = KnowledgeGraphManager(server.data_root)
    root = graph_manager.knowledge_dir(workspace)
    graph = graph_manager.load_graph(project_id, root)
    common = {"class": "fact", "title": "Existing knowledge", "one_liner": "Existing evidence", "body_md": "Legacy evidence body",
        "status": "core", "needs_review": False, "evidence": [], "usage_events": [], "metrics": {},
        "source_ids": [], "course_ids": [], "created_at": "2026-10-04T00:00:00Z", "updated_at": "2026-10-04T00:00:00Z"}
    graph["assets"] = {
        "F-01": {**common, "id": "F-01", "scope": "project", "scope_set_by": "system", "course_ids": ["calculus"]},
        "F-02": {**common, "id": "F-02", "scope": "global", "scope_set_by": "user"},
        "F-03": {**common, "id": "F-03", "scope": "project", "scope_set_by": "system"},
    }
    graph_manager.save_graph(root, graph)
    assets = KnowledgeAssetManager(graph_manager)
    register_project(project_id, manager=graph_manager, assets=assets, workspace_provider=lambda: workspace)
    try:
        broad_path = "/api/knowledge/assets?" + urlencode({"group_id": group.group_id, "legacy_project_id": project_id})
        before = await http(broad_path)
        assert before[0] == 200 and {row["asset_id"] for row in before[1]["assets"]} == {"F-01", "F-02"}
        assert (await http("import", document(), group_id=group.group_id, course="calculus"))[0] == 200
        after = await http(broad_path)
        assert after[0] == 200 and {row["asset_id"] for row in after[1]["assets"]} == {"F-01", "F-02"}
        narrow = await http(broad_path + "&course_id=calculus")
        assert narrow[0] == 200 and {row["asset_id"] for row in narrow[1]["assets"]} == {"F-01"}
        assert (await http(broad_path + "&course_id=unbound"))[0] == 403
    finally:
        unregister_project(project_id)


async def test_real_route_rejects_shared_string_amplification_without_any_publication(app, tmp_path):
    from backend.tests.test_rag_import_budgets import amplified_workbook
    server, group, _, http = app
    existing = (await http("import", document()))[1]["source"]["source_id"]
    before = server.groups.get(group.group_id).to_dict()
    raw = amplified_workbook(tmp_path)
    status, body = await http("import", {"filename": "small.xlsx", "data_base64": base64.b64encode(raw).decode("ascii")})
    assert status == 400 and body["error"] == "invalid_local_document_or_request"
    assert server.groups.get(group.group_id).to_dict() == before
    assert server.local_knowledge.store.document_ids() == (existing,)
    assert (await http("search", {"query": "导数"}))[1]["results"]


@pytest.mark.parametrize("limit", ["parsed_text", "chunks", "chunk_text"])
async def test_final_publication_guards_leave_no_encrypted_rows_or_source_links(app, monkeypatch, limit):
    from backend.knowe_rag.parsers import ParsedBlock
    server, group, _, http = app
    if limit == "parsed_text":
        monkeypatch.setattr(local_knowledge, "parse_document_bytes", lambda *_: (
            ParsedBlock(text="x" * (local_knowledge.MAX_EXTRACTED_CHARS + 1), location="para:1", kind="paragraph"),))
        monkeypatch.setattr(local_knowledge, "build_chunks", lambda *_args, **_kwargs: pytest.fail("Oversized output reached chunking"))
    elif limit == "chunks":
        monkeypatch.setattr(local_knowledge, "MAX_PUBLICATION_CHUNKS", 1)
    else:
        monkeypatch.setattr(local_knowledge, "MAX_PUBLICATION_TEXT_CHARS", 1)
    assert (await http("import", document()))[0] == 400
    assert server.groups.get(group.group_id).source_links == []
    assert server.local_knowledge.store.document_ids() == ()
    with sqlite3.connect(server.local_knowledge.store.path) as db:
        for table in ("rag_documents", "rag_chunks", "rag_sources"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


async def test_chunk_preparation_is_off_loop_and_scope_is_rechecked_before_publication(app, monkeypatch):
    server, group, _, http = app
    entered, release = threading.Event(), threading.Event()
    real_build = local_knowledge.build_chunks

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return real_build(*args, **kwargs)

    monkeypatch.setattr(local_knowledge, "build_chunks", delayed)
    pending = asyncio.create_task(http("import", document(), course="calculus"))
    assert await asyncio.to_thread(entered.wait, 5)
    # A listener request must still be serviceable while chunk preparation is paused.
    assert (await http("sources"))[0] == 200
    server.groups.unbind_course(group.group_id, "calculus")
    release.set()
    assert (await pending)[0] == 403
    assert server.local_knowledge.store.document_ids() == ()


async def test_local_answer_exact_source_version_quote_and_hallucination_abstention(app):
    server, group, _, http = app
    source = (await http("import", document()))[1]["source"]
    status, result = await http("answer", {"query": "导数"})
    assert status == 200 and result["verification"] == "exact_quotes_verified"
    citation = result["citations"][0]
    assert citation["source_id"] == source["source_id"] and citation["version"] == 1
    assert citation["quote"] in result["answer"]
    async def hallucinate(**kwargs):
        return {"answer": "Invented", "citations": [{**citation, "quote": "not in document"}]}
    server.local_knowledge.answer_reader = hallucinate
    bad = (await http("answer", {"query": "导数", "use_model": True, "approved": True}))[1]
    assert bad["verification"] == "abstain_unsupported_citation" and not bad["citations"]
    assert (await http("answer", {"query": "导数", "use_model": True}))[0] == 403


async def test_visual_reader_is_version_bound_checkpoints_and_resumes(app):
    server, _, _, http = app
    raw = b"\x89PNG\r\n\x1a\nfixture"
    source = (await http("import", {"filename": "image.png", "data_base64": base64.b64encode(raw).decode("ascii")}))[1]["source"]
    calls = []
    async def reader(**kwargs):
        calls.append(kwargs)
        return "导数 visual evidence"
    server.local_knowledge.vision_reader = reader
    assert (await http("vision", {"source_id": source["source_id"], "approved": True, "source_version": 0}))[0] == 403
    status, visual = await http("vision", {"source_id": source["source_id"], "approved": True, "source_version": 1})
    assert status == 200 and visual["source"]["version"] == 2 and visual["remaining_pages"] == 0
    assert calls[0]["authorization"]["source_version"] == 1
    assert (await http("search", {"query": "导数"}))[1]["results"]
    assert (await http("vision", {"source_id": source["source_id"], "approved": True, "source_version": 2}))[0] == 200
    assert len(calls) == 1
    detail = (await http("detail", extra={"source_id": source["source_id"]}))[1]
    assert "vision_text" not in detail["source"]


async def test_production_rag_composition_approval_mock_egress_and_exact_citations(app, monkeypatch):
    from backend import runtime_settings, commercial_gateway
    from backend.tests.test_rag_vision_egress import _client
    import httpx
    server, group, _, http = app
    calls = []
    def handler(request):
        calls.append(request)
        wire = json.loads(request.content)
        evidence = json.loads(wire["messages"][-1]["content"])["evidence"][0]
        citation = {k: evidence[k] for k in ("source_id", "chunk_id", "version", "location")}
        citation["quote"] = evidence["text"]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"answer": evidence["text"], "citations": [citation]}, ensure_ascii=False)}}], "usage": {"prompt_tokens": 30, "completion_tokens": 10}})
    class FixtureGateway(commercial_gateway.ProviderGatewayClient):
        def __new__(cls, **kwargs):
            client = _client(handler, model="qwen3.8")
            client.authority_provider = kwargs["authority_provider"]
            return client
    monkeypatch.setattr(commercial_gateway, "ProviderGatewayClient", FixtureGateway)
    monkeypatch.setattr(runtime_settings, "model_binding_for", lambda *_: {"provider": "alibaba", "model": "qwen3.8", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "transport": "openai_chat", "secret_ref": "fixture", "credential_purpose": "fixture"})
    await http("import", document())
    payload = {"query": "导数", "use_model": True, "approved": True}
    status, pending = await http("answer", payload)
    assert status == 202 and pending["approval_required"] and not calls
    approved_status, approved = await http("/task-runtime/approval", {"group_id": group.group_id, "approval_id": pending["approval_id"], "approved": True, "capabilities": ["provider_egress"], "request_key": "rag-test-approve"})
    assert approved_status == 200
    pending_again = (await http("sources"))[1]["pending_operations"]
    assert pending_again[0]["operation_id"] == pending["operation_id"]
    result_status, result = await http("answer", {**payload, "authorization": {"operation_id": pending["operation_id"]}})
    assert result_status == 200 and result["verification"] == "exact_quotes_verified" and len(calls) == 1
    assert server.task_runtime.store.get_run(pending["run_id"]).status.value == "partial"
    duplicate_status, duplicate = await http("answer", {**payload, "authorization": {"operation_id": pending["operation_id"]}})
    assert duplicate_status == 200 and duplicate["answer"] == result["answer"] and len(calls) == 1


async def test_card_source_guard_revision_tracks_atomic_source_version_and_scoped_tool(app):
    from backend.local_knowledge import LocalKnowledgeCardSourceGuard
    server, group, other, http = app
    source = (await http("import", document()))[1]["source"]
    guard = LocalKnowledgeCardSourceGuard(server.local_knowledge)
    with guard.guard(server.groups.get(group.group_id)) as before:
        evidence = guard.evidence(group.group_id, None, "导数")
        assert evidence[0]["source_id"] == source["source_id"]
        assert guard.evidence(other.group_id, None, "导数") == []
    metadata = server.local_knowledge.store.source_metadata(source["source_id"])
    metadata["version"] = 2
    server.local_knowledge.store.write_document(source["source_id"], server.local_knowledge.store.read_chunks(source["source_id"]), metadata=metadata, expected_version=1)
    with guard.guard(server.groups.get(group.group_id)) as after:
        assert before.revision != after.revision
        assert guard.evidence(group.group_id, None, "导数")[0]["version"] == 2


async def test_office_visual_conversion_keeps_original_text_and_uses_pdf_page_vision(app, monkeypatch, tmp_path):
    from backend.tests.test_rag_office_parsers import _docx, _p
    raw = _docx(tmp_path / "synthetic.docx", _p("Original textual evidence")).read_bytes()
    server, _, _, http = app
    source = (await http("import", {"filename": "synthetic.docx", "data_base64": base64.b64encode(raw).decode("ascii")}))[1]["source"]
    pdf = pdfium.PdfDocument.new(); pdf.new_page(100, 100)
    buffer = BytesIO(); pdf.save(buffer); pdf.close()
    monkeypatch.setattr(local_knowledge, "convert_office_to_pdf", lambda *_: buffer.getvalue())
    calls = []
    async def reader(**kwargs):
        calls.append(kwargs)
        return "Synthetic chart A=12 B=27"
    server.local_knowledge.vision_reader = reader
    status, result = await http("vision", {"source_id": source["source_id"], "source_version": 1, "approved": True, "page_limit": 1})
    assert status == 200 and calls[0]["location"] == "page:1"
    assert calls[0]["image"].startswith(b"\x89PNG")
    assert (await http("search", {"query": "Original"}))[1]["results"]
    assert (await http("search", {"query": "chart"}))[1]["results"]
