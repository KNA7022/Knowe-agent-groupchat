"""Real React task UI -> authenticated KnoweServer HTTP -> protected Worker probe.

The unchanged production component is mounted in an isolated Vite page. This
proves its real HTTP flow; it does not claim Electron shell/navigation coverage.
The caller alone supplies credentials and a network-budgeted client factory.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import uuid


async def run_ui_probe(data_root, binding, credential, client_factory):
    from playwright.async_api import async_playwright
    from backend import config, runtime_settings, server as server_module
    from backend.commercial_gateway import derive_provider_credential_purpose
    from backend.task_runtime.product import ProtectedProduct

    root = Path(data_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError("UI probe requires a fresh isolated data directory")
    repo = Path(__file__).resolve().parents[1]
    previous = (config.CONFIG, server_module.CONFIG, runtime_settings._state,
                runtime_settings._loaded, runtime_settings._secret_lease_broker,
                os.environ.get("ELECTRON_RENDERER_URL"))
    token = uuid.uuid4().hex + uuid.uuid4().hex
    suffix = uuid.uuid4().hex
    html_path = repo / f".live-ui-{suffix}.html"
    node_path = repo / f".live-ui-{suffix}.mjs"
    listener = node = browser = app = None
    steps = []
    http_statuses = []
    screenshots = []
    try:
        settings = replace(config.CONFIG, data_dir=str(root), runtime_token=token,
                           health_host="127.0.0.1", health_port=0)
        config.CONFIG = server_module.CONFIG = settings
        runtime_settings._state = runtime_settings._default_state()
        runtime_settings._loaded = True
        app = server_module.KnoweServer(data_dir=str(root))
        selected = dict(binding)
        selected.update(secret_ref="secret_" + uuid.uuid4().hex, credential_slot="main",
                        credential_kind="provider_credential", credential_audience="provider_gateway")
        for name in ("api_key", "key", "credential", "secret", "token"):
            selected.pop(name, None)
        selected["credential_purpose"] = derive_provider_credential_purpose(
            slot="main", provider_id=selected["provider"], endpoint=selected["base_url"],
            transport=selected["transport"])
        runtime_settings.apply({"main_model": selected})
        app.secret_leases.provision(lease_id="lease_" + uuid.uuid4().hex,
            secret_ref=selected["secret_ref"], slot="main", kind="provider_credential",
            purpose=selected["credential_purpose"], audience="provider_gateway",
            provider_id=selected["provider"], endpoint=selected["base_url"],
            transport=selected["transport"], value=credential,
            expires_at=(datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat())
        group = app.groups.create_group("Live UI synthetic task", optional_roles=("knowledge",),
                                        legacy_project_id="project_20261011000992")
        original_engine_for = app.engine_for
        def bounded_engine_for(project_id, project_name=None):
            engine = original_engine_for(project_id, project_name)
            # Every engine created through the real server uses caller-owned budget HTTP.
            engine._client_factory = client_factory
            return engine
        app.engine_for = bounded_engine_for
        listener = await asyncio.start_server(app._health_conn, "127.0.0.1", 0)
        http_port = listener.sockets[0].getsockname()[1]
        http_base = f"http://127.0.0.1:{http_port}"
        html_path.write_text('''<!doctype html><html lang="zh"><meta charset="utf-8">
<title>Protected production task UI probe</title><div id="root"></div>
<script type="module">
import React from 'react';
import {createRoot} from 'react-dom/client';
import {ProtectedTaskPanel} from '/src/components/ProtectedTaskPanel.tsx';
import '/src/styles/knowe-tokens.css';
import '/src/styles/knowe-components.css';
createRoot(document.getElementById('root')).render(React.createElement(ProtectedTaskPanel,
  {groupId:''' + json.dumps(group.group_id) + '''}));
</script></html>''', encoding="utf-8")
        # Vite owns React transforms and serves actual source; APIs are never intercepted.
        node_path.write_text('''import {createServer} from 'vite';
import react from '@vitejs/plugin-react';
const server=await createServer({configFile:false,root:process.cwd(),plugins:[react()],
server:{host:'127.0.0.1',port:0,strictPort:false,hmr:false}});
await server.listen();
process.stdout.write(JSON.stringify({port:server.httpServer.address().port})+'\\n');
process.stdin.resume();
process.stdin.on('end',async()=>{await server.close();process.exit(0);});
''', encoding="utf-8")
        pinned_node = repo/'.tools/node-v24.9.0-win-x64/node.exe'
        executable = str(pinned_node) if pinned_node.is_file() else shutil.which("node")
        if not executable:
            raise RuntimeError("Node.js is required for the production Vite UI probe")
        node = await asyncio.create_subprocess_exec(executable, str(node_path), cwd=str(repo),
            env={k:v for k,v in os.environ.items() if k!='STUDYCREW_EVAL_API_KEY'},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        for _ in range(20):
            line=await asyncio.wait_for(node.stdout.readline(),timeout=30)
            if not line:
                raise RuntimeError('vite_probe_exited_before_ready')
            try:
                port_record=json.loads(line)
                if isinstance(port_record,dict) and 'port' in port_record: break
            except ValueError:
                continue
        else:
            raise RuntimeError('vite_probe_readiness_not_received')
        ui_base = f"http://127.0.0.1:{port_record['port']}"
        os.environ["ELECTRON_RENDERER_URL"] = ui_base
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1100, "height": 900}, locale="zh-CN")
            await page.add_init_script("window.knowe={runtimeEndpoints:" + json.dumps({
                "httpBase": http_base, "wsUrl": "ws://127.0.0.1:1"}) +
                ",getRuntimeToken:async()=>" + json.dumps(token) + "};")
            page.on("response", lambda response: http_statuses.append({
                "path": response.url.split(http_base, 1)[-1].split("?", 1)[0], "status": response.status})
                if response.url.startswith(http_base) else None)
            # Prevent renderer fetches to any remote origin; provider traffic belongs to
            # the server's governed client factory, never the browser.
            async def local_only(route):
                if route.request.url.startswith((ui_base, http_base, "data:", "blob:")):
                    await route.continue_()
                else:
                    await route.abort()
            await page.route("**/*", local_only)
            await page.goto(f"{ui_base}/{html_path.name}", wait_until="networkidle", timeout=30000)
            panel = page.get_by_role("region", name="受保护群组任务")
            await panel.wait_for(timeout=30000)
            steps.append("production_react_panel_loaded")
            instruction = "Reply in exactly one sentence: Spaced review revisits learning material at increasing intervals."
            await panel.get_by_label("任务指令").fill(instruction)
            await panel.get_by_role("button", name="生成审批提案", exact=True).click()
            await panel.get_by_role("button", name="批准并执行此版本", exact=True).wait_for(timeout=30000)
            assert await panel.get_by_text(instruction, exact=True).count() == 1
            assert app.task_runtime.store.list_runs(group_id=group.group_id)[0].budget_usage.model_turns == 0
            steps.append("input_proposal_exact_version_no_provider_call")
            proposal_shot = root / "ui-proposal.png"
            await page.screenshot(path=str(proposal_shot), full_page=True)
            screenshots.append(str(proposal_shot))
            await panel.get_by_role("button", name="批准并执行此版本", exact=True).click()
            steps.append("user_clicked_exact_version_approval")
            await panel.get_by_text("任务结果 · 已完成", exact=True).wait_for(timeout=150000)
            snapshot = ProtectedProduct(app.task_runtime, app.engine_for).snapshot(group.group_id)
            run = app.task_runtime.store.list_runs(group_id=group.group_id)[0]
            assert snapshot["runs"][0]["status"] == "succeeded"
            content = snapshot["messages"][0]["content"]
            assert content and await panel.get_by_text(content, exact=True).count() >= 1
            assert 1 <= run.budget_usage.model_turns <= 3
            assert not await panel.get_by_role("alert").count()
            steps.append("real_worker_verified_result_rendered")
            result_shot = root / "ui-result.png"
            await page.screenshot(path=str(result_shot), full_page=True)
            screenshots.append(str(result_shot))
            # Reload proves displayed result is backed by real authenticated persistence.
            await page.reload(wait_until="networkidle")
            await page.get_by_text(content, exact=True).wait_for(timeout=30000)
            assert app.task_runtime.store.list_runs(group_id=group.group_id)[0].budget_usage.model_turns == run.budget_usage.model_turns
            steps.append("browser_reload_restores_result_without_reexecution")
            await browser.close()
            browser = None
            return {"ok": True, "scope": "production_react_component_real_server_http_and_worker",
                    "status": "succeeded", "model_calls": run.budget_usage.model_turns,
                    "tool_calls": run.budget_usage.tool_calls, "steps": steps,
                    "screenshots": screenshots, "http": http_statuses, "snapshot": snapshot}
    finally:
        if browser is not None:
            await browser.close()
        if listener is not None:
            listener.close()
            await listener.wait_closed()
        if node is not None:
            if node.stdin:
                node.stdin.close()
            try:
                await asyncio.wait_for(node.wait(), timeout=10)
            except asyncio.TimeoutError:
                node.terminate()
                await node.wait()
        html_path.unlink(missing_ok=True)
        node_path.unlink(missing_ok=True)
        if app is not None:
            for engine in app.engines.values():
                await engine.stop(immediate=True)
            await app._stop_task_runtime_event_bridge()
            if app.local_knowledge is not None:
                app.local_knowledge.store.close()
            app.task_runtime.close()
            app.secret_leases.close()
            if app.store is not None:
                await asyncio.to_thread(app.store.close)
        config.CONFIG, server_module.CONFIG, runtime_settings._state, runtime_settings._loaded, runtime_settings._secret_lease_broker, previous_origin = previous
        if previous_origin is None:
            os.environ.pop("ELECTRON_RENDERER_URL", None)
        else:
            os.environ["ELECTRON_RENDERER_URL"] = previous_origin
