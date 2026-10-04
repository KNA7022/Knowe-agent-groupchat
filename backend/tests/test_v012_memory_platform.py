"""
[v0.12 D] 问题五 5d/5e + 问题六 6c 的回归测试 —— memory_manager / platform，均可独立运行。
"""
import sys, os, time, asyncio
import pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.memory_manager import MemoryManager
from backend.platform_manifest import PlatformManifest


# ── 6c：辅助 LLM 不可用也能生成 context.md ──────────────────────
def test_project_memory_generated_without_llm(tmp_path):
    d = str(tmp_path)
    m = MemoryManager(d, aux_call=None)          # 无 LLM
    ws = os.path.join(d, "ws")
    m.ensure_project_context(ws, members=0)
    assert m.project_context_path(ws).is_file(), "ensure 后 context.md 必须存在"

    async def run():
        await m.update_project_context(
            ws, {"final_response": "完成了登录页 UI，交了 report。"},
            turn_count=1, members=2)
    asyncio.run(run())
    txt = m.project_context_path(ws).read_text(encoding="utf-8")
    assert "登录页" in txt, "无 LLM 时应把原文摘录写进最近活动（6c）"


# ── 6a：harness 文件落在 data/harness/ 子目录 ──────────────────
def test_harness_in_subdir(tmp_path):
    d = str(tmp_path)
    m = MemoryManager(d)
    async def run():
        await m.update_harness([{"project_id": "p1", "name": "官网", "members": 3, "recent": "写接口"}])
    asyncio.run(run())
    assert m.harness_path.is_file()
    assert m.harness_path.parent.name == "harness", "harness_memory.md 应在 data/harness/"


# ── 5d：brief 比 full 短很多，且信息完整 ────────────────────────
def test_harness_brief_compact(tmp_path):
    d = str(tmp_path)
    m = MemoryManager(d)
    projs = [{"project_id": f"p{i}", "name": f"项目{i}", "members": i, "recent": f"动态{i}"}
             for i in range(5)]
    async def run():
        await m.update_harness(projs)
    asyncio.run(run())
    brief = m.read_harness_brief()
    full = m.read_harness()
    assert len(brief) < len(full)
    for i in range(5):
        assert f"项目{i}" in brief          # 信息没丢，只是更省 token


# ── 5e：平台清单——首次安装 + 变更追踪 + 版本升级 ────────────────
def test_platform_manifest_lifecycle(tmp_path):
    data = str(tmp_path / "data")
    install = str(tmp_path / "install")
    os.makedirs(os.path.join(install, "node_modules", "x"))
    with open(os.path.join(install, "a.py"), "w", encoding="utf-8") as stream:
        stream.write("x=1\n")
    with open(os.path.join(install, "node_modules", "x", "junk.js"), "w", encoding="utf-8") as stream:
        stream.write("noise\n")

    pm = PlatformManifest(data, install, "0.12")
    pm.refresh()
    assert pm.manifest_path.is_file() and pm.changelog_path.is_file()
    assert "首次安装" in pm.changelog_path.read_text(encoding="utf-8")

    time.sleep(0.01)
    with open(os.path.join(install, "b.py"), "w", encoding="utf-8") as stream:
        stream.write("y=2\n")   # 新增
    pm.refresh()
    assert "新增" in pm.changelog_path.read_text(encoding="utf-8")

    snap = pm._load_snapshot()
    assert not any("node_modules" in k for k in snap), "node_modules 应被跳过"

    # 版本升级入日志
    pm2 = PlatformManifest(data, install, "0.13")
    pm2.refresh()
    assert "0.12 → 0.13" in pm2.changelog_path.read_text(encoding="utf-8")

    # brief 里有真实关键路径（回答「公告栏存哪」）
    brief = pm2.read_brief()
    assert "harness_memory.md" in brief and "0.13" in brief


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
