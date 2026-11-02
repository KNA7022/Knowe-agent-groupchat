"""
hub.py — 事件总线：项目注册表 / seq 单点盖号 / ring / 广播 / 客户端管理。

三条铁律（PROTOCOL.md §a）：
  1. **seq 由 server 单点加锁分配，按项目独立递增**——引擎不许自己盖号。
  2. **所有事件广播给所有客户端**（含别的项目的事件、含发送者自己的 user_echo）。
     前端按 project_id 路由后再做水位/去重。
  3. **无 seq 白名单事件绝不盖号**（project_created / pong / replay_complete / resync_required /
     project_directory_required / project_directory_restored / 服务器级 error），否则前端水位会被污染。

BUG-1 的根治：剪枝死连接一律用 `ws.state is State.OPEN`。
  新版 websockets（≥14）没有 `.open` 属性——`getattr(c, 'open', False)` 会恒为 False，
  于是「所有连接都被当成死的」→ 广播失聪。这个坑不再有第二次机会：
  本文件是唯一判活的地方，函数名叫 `_is_alive`，全仓 grep 得到。
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections import OrderedDict, deque
from typing import Any, Callable, Iterable

from websockets.protocol import State

from .config import CONFIG
from .contract import (
    NO_SEQ_EVENT_TYPES,
    STRUCTURAL_EVENT_TYPES,
    UNREAD_EVENT_TYPES,
    ContractViolation,
    now_ts,
)
from .ring import RingBuffer
from .privacy import sanitize_event

log = logging.getLogger("knowe.hub")


# ═══════════════════════════════════════════════════════════════
# 客户端
# ═══════════════════════════════════════════════════════════════

class Client:
    """一条 WebSocket 连接。每个客户端记住自己收过哪些 project_created。"""

    _next_id = 0
    LIVE_REPLAY_BUFFER_MAX = 4096
    TASK_RUNTIME_RECOVERY_WINDOW_MAX = 4

    def __init__(self, ws: Any) -> None:
        Client._next_id += 1
        self.id = f"c{Client._next_id}"
        self.connected_at_ns = time.time_ns()
        self.ws = ws
        self._send_lock = asyncio.Lock()
        self._protected_hydrate = None
        self._ordered_events: deque[dict[str, Any]] = deque()
        self._ordered_task = None
        self._protected_resync_projects: set[str] = set()
        self._protected_recovery_snapshots: dict[str, dict[str, Any]] = {}
        self.task_runtime_recovery_fenced = False
        self.task_runtime_recovery_session_id = secrets.token_urlsafe(24)
        self.task_runtime_recovery_disconnect_debt_transferred = False
        self.task_runtime_recovery_debt_retry_count = 0
        # Socket teardown is a two-phase retirement.  A fenced session is hidden
        # from live delivery, but remains in ``Hub.clients`` until its uncovered
        # recovery suffix has been transferred durably.  The server owns the one
        # reconciler and completes removal exactly once.
        self.task_runtime_recovery_retirement_complete = False
        self.task_runtime_recovery_debt_reconcile_task: asyncio.Task[Any] | None = None
        # ``-1`` means this Client bypassed the server admission boundary (tests or
        # legacy callers) and therefore cannot claim any disconnect debt.
        self.task_runtime_recovery_disconnect_epoch_baseline = -1
        # ★ 每客户端独立的「已发过 project_created 的项目集合」——握手时按此补发
        self.sent_projects: set[str] = set()
        self.handshake_done = False
        # Live Hub broadcasts are fenced until replay_complete has handed over an
        # authoritative snapshot/cursor. Point-to-point replay frames bypass this queue.
        self._live_replay_fenced = True
        self._live_replay_buffer: deque[dict[str, Any]] = deque()
        self._live_replay_overflow_projects: set[str] = set()
        # Recovery ACKs are accepted only for a snapshot cursor issued to this
        # authenticated live connection.  Reconnect intentionally starts empty.
        # One recovery key owns exactly one mutually-exclusive phase.  The union of
        # these collections is the hard per-connection window; applied entries exist
        # only for out-of-order ACK compaction and can never grow outside that window.
        self.task_runtime_recovery_reserved: set[tuple[str, str, int]] = set()
        self.task_runtime_recovery_notified: set[tuple[str, str, int]] = set()
        self.task_runtime_recovery_snapshots: dict[tuple[str, str, int], str] = {}
        self.task_runtime_recovery_applied: set[tuple[str, str, int]] = set()
        self.task_runtime_recovery_revisions: dict[tuple[str, str, int], int] = {}
        self.task_runtime_recovery_kinds: dict[tuple[str, str, int], str] = {}
        self.task_runtime_recovery_order: deque[tuple[str, str, int]] = deque()
        self.task_runtime_recovery_ack_revision = 0
        self.task_runtime_recovery_update_order: deque[tuple[str, str, int]] = deque()
        self.task_runtime_recovery_update_ack_revision = 0
        self.task_runtime_recovery_update_after_revision = 0
        # A successful notice starts a bounded renderer-ACK lease. Only contiguous
        # base/update progress renews it; transport liveness alone is insufficient.
        self.task_runtime_recovery_last_progress_at = time.monotonic()
        self.task_runtime_recovery_progress_deadline: float | None = None
        self.task_runtime_recovery_send_tasks: dict[
            tuple[str, str, int], asyncio.Task[bool]
        ] = {}
        self.task_runtime_recovery_send_deadlines: dict[
            tuple[str, str, int], float
        ] = {}
        # Recovery intents stay durable in SQLite.  A connection holds only a small
        # fair-scanning window and one opaque paging cursor, never a mirror of the
        # complete recovery table.
        self.task_runtime_recovery_after_group_id = ""  # legacy diagnostics only
        self.task_runtime_recovery_after_revision = 0
        self.task_runtime_recovery_page_exhausted = False
        self.task_runtime_recovery_seen_revision = 0
        self.task_runtime_recovery_fill_lock = asyncio.Lock()

    @property
    def alive(self) -> bool:
        return not self.task_runtime_recovery_fenced and _is_alive(self.ws)

    @property
    def task_runtime_recovery_slot_count(self) -> int:
        """Hard-window accounting across mutually-exclusive recovery phases."""
        phases = (
            self.task_runtime_recovery_reserved,
            self.task_runtime_recovery_notified,
            set(self.task_runtime_recovery_snapshots),
            self.task_runtime_recovery_applied,
        )
        flattened = [key for phase in phases for key in phase]
        if len(flattened) != len(set(flattened)):
            raise RuntimeError("Task Runtime recovery key owns multiple phases")
        return len(flattened)

    async def send(self, event: dict[str, Any], *, _protected_history: bool = False) -> bool:
        if not self.alive:
            return False
        protected_send = False
        try:
            async with self._send_lock:
                from .protected_events import REF
                from .task_runtime.group_content import REF as CHAT_REF
                def contains(value):
                    if isinstance(value, dict):
                        return REF in value or CHAT_REF in value or any(contains(child) for child in value.values())
                    return isinstance(value, list) and any(contains(child) for child in value)
                if contains(event):
                    protected_send = True
                    if self._protected_hydrate is None:
                        return False
                    # Hydration occurs after backpressure, never before the send lock.
                    event = self._protected_hydrate(event, history=_protected_history)
                await self.ws.send(json.dumps(event, ensure_ascii=False))
            return True
        except Exception as exc:  # 连接在发的过程中断了
            log.debug("send failed to %s: %s", self.id, type(exc).__name__ if protected_send else exc)
            return False

    def enqueue_ordered(self, event: dict[str, Any]) -> None:
        project = event.get("project_id", "")
        recovery = project in self._protected_resync_projects and event.get("type") == "state_snapshot"
        if (project in self._protected_resync_projects and not recovery
                and event.get("type") != "task_runtime_event"
                and project not in self._protected_recovery_snapshots):
            return
        if len(self._ordered_events) >= self.LIVE_REPLAY_BUFFER_MAX:
            self.request_resync(project, event.get("seq", 0))
            return
        queued = dict(event)
        if recovery:
            # The stream-unique snapshot seq is the recovery generation. Later live
            # refs queue behind it; a new resync invalidates this pending handoff.
            self._protected_recovery_snapshots.setdefault(project, queued)
        self._ordered_events.append(queued)
        self._start_ordered_drain()

    def _start_ordered_drain(self):
        if self._ordered_task is None or self._ordered_task.done():
            self._ordered_task = asyncio.create_task(self._drain_ordered())

    async def _drain_ordered(self):
        while self._ordered_events:
            event = self._ordered_events.popleft()
            project = event.get("project_id", "")
            recovery = self._protected_recovery_snapshots.get(project)
            if event.get("type") == "resync_required":
                await self.send(event)
            elif event is recovery:
                # Do not treat initial replay buffering as a successful handoff.
                sent = await self.send(event)
                if self._protected_recovery_snapshots.get(project) is not recovery:
                    continue  # A newer failure won while the socket was awaiting.
                if sent:
                    self._protected_recovery_snapshots.pop(project, None)
                    self._protected_resync_projects.discard(project)
                else:
                    self.request_resync(project, event.get("seq", 0), failed_handoff=True)
            elif not await self.send_live(event):
                self.request_resync(project, event.get("seq", 0))

    def request_resync(self, project, seq, *, failed_handoff=False):
        already_fenced = project in self._protected_resync_projects
        self._protected_resync_projects.add(project)
        invalidated = self._protected_recovery_snapshots.pop(project, None)
        self._ordered_events = deque(e for e in self._ordered_events
            if e.get("project_id") != project or e.get("type") in {"resync_required", "task_runtime_event"})
        self._live_replay_buffer = deque(e for e in self._live_replay_buffer
            if e.get("project_id") != project or e.get("type") == "task_runtime_event")
        # Overflow/new invalidation supersedes an apparently successful in-flight
        # snapshot: its client needs one later notice. Hydration denial must not
        # recursively trigger automatic snapshot retries on a healthy socket.
        notify = not already_fenced or (invalidated is not None and not failed_handoff)
        if notify and not any(e.get("type") == "resync_required" and e.get("project_id") == project
                              for e in self._ordered_events):
            self._ordered_events.append({"type": "resync_required", "project_id": project,
                "last_seq": seq, "message": "Committed history requires replay"})
        self._start_ordered_drain()

    async def send_live(self, event: dict[str, Any]) -> bool:
        """Buffer broadcasts until replay/snapshot handoff completes for this client."""
        # Task Runtime has an independent authoritative status/recovery journal.
        # A private-history hydration fence must not hide a truthful terminal status.
        if (event.get("project_id") in self._protected_resync_projects
                and event.get("type") != "task_runtime_event"):
            return False
        if self._live_replay_fenced:
            if len(self._live_replay_buffer) >= self.LIVE_REPLAY_BUFFER_MAX:
                project_id = event.get("project_id")
                if isinstance(project_id, str):
                    self._live_replay_overflow_projects.add(project_id)
                return True
            self._live_replay_buffer.append(dict(event))
            return True
        return await self.send(event)

    async def release_live_replay_fence(self) -> set[str]:
        """Drain the ordered handoff queue, then atomically switch to direct live sends."""
        while self._live_replay_buffer:
            event = self._live_replay_buffer.popleft()
            if (event.get("project_id") in self._protected_resync_projects
                    and event.get("type") != "task_runtime_event"):
                continue
            if not await self.send(event):
                if self._protected_hydrate is not None:
                    self.request_resync(event.get("project_id", ""), event.get("seq", 0))
                else:
                    self._live_replay_buffer.clear()
                    break
        self._live_replay_fenced = False
        overflow = set(self._live_replay_overflow_projects)
        self._live_replay_overflow_projects.clear()
        return overflow


def _is_alive(ws: Any) -> bool:
    """★ 唯一判活口径（BUG-1）：websockets≥14 用 State.OPEN，没有 .open 属性。"""
    return getattr(ws, "state", None) is State.OPEN


# ═══════════════════════════════════════════════════════════════
# 项目
# ═══════════════════════════════════════════════════════════════

class Project:
    def __init__(self, project_id: str, name: str, ring_capacity: int) -> None:
        self.id = project_id
        self.name = name
        self.seq = 0                       # 已盖出的最大 seq（0 = 还没有事件）
        self.ring = RingBuffer(ring_capacity)
        self.last_read_seq = 0             # 已读水位（客户端上报）
        self.members: list[dict[str, str]] = []
        self.pending_card: dict[str, Any] | None = None

    # unread_count 已上移到 Hub.unread_count()（v1.0.35.3）：未读须按「消息数」算，
    # 消息全集在 durable_conversation（Hub 层有 store 才能读盘），Project 只有 ring。


# ═══════════════════════════════════════════════════════════════
# Hub
# ═══════════════════════════════════════════════════════════════

class Hub:
    IDEMPOTENCY_CACHE_MAX = 4096

    def __init__(self, store: Any | None = None) -> None:
        self.projects: dict[str, Project] = {}
        self.clients: set[Client] = set()
        self._seq_lock = asyncio.Lock()    # ★ 单点盖号锁
        # [v0.4] 落盘钩子（backend.persist.Store）。None = 纯内存，行为和 v0.3 一模一样。
        #   放在这里而不是放进 emit 的调用方，是因为 emit 是**唯一**的出站口——
        #   挂在这里，就不存在「某条事件忘了存」这回事。
        self.store = store
        # project_id → 用户可见文本过滤器。由 ProjectEngine 注册，Hub 是最终强制出口。
        self._public_text_filters: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}
        # project_id -> deterministic projection event_id -> committed envelope.
        # The cache is backed by the durable event log on first lookup so outbox replay
        # after a process restart cannot create a second user/UI message.
        self._idempotent_events: dict[str, OrderedDict[str, dict[str, Any]]] = {}
        # Migration-window identity adapter.  The runtime remains keyed by its stable
        # historical project_id, while transport envelopes additionally expose the
        # canonical Group identity.  The server installs this resolver after startup.
        self.group_id_resolver: Callable[[str], str | None] | None = None
        self.protected_events = None
        self.group_content = None
        self.protected_enqueue_fault = None

    def bind_protected_events(self, authority):
        if self.protected_events is not None and self.protected_events is not authority:
            raise RuntimeError("protected event authority already bound")
        self.protected_events = authority
        if self.store is not None:
            self.store.protected_events = authority
        authority._invalidators.append(self._invalidate_protected_caches)

    def _invalidate_protected_caches(self, task_id):
        # References contain no content. Drop transient copies after the durable fence;
        # authoritative replay rebuilds unaffected histories without decoding them.
        from .protected_events import REF
        def affected(value):
            if isinstance(value, dict):
                return ((REF in value and value.get("task_id") == task_id)
                    or any(affected(child) for child in value.values()))
            return isinstance(value, list) and any(affected(child) for child in value)
        for cache in self._idempotent_events.values():
            for key, value in tuple(cache.items()):
                if affected(value):
                    del cache[key]
        for project in self.projects.values():
            retained = [e for e in project.ring.events() if not affected(e)]
            project.ring = RingBuffer(CONFIG.ring_capacity)
            for event in retained:
                project.ring.append(event)
            if affected(project.pending_card):
                project.pending_card = None
        for client in self.clients:
            for project, snapshot in tuple(client._protected_recovery_snapshots.items()):
                if affected(snapshot):
                    client.request_resync(project, snapshot.get("seq", 0))
            client._live_replay_buffer = deque(e for e in client._live_replay_buffer if not affected(e))
            client._ordered_events = deque(e for e in client._ordered_events if not affected(e))

    async def emit_protected(self, access, outbox_id):
        """Commit a completion-derived message; no notification/recipient ACK claim."""
        authority = self.protected_events
        if authority is None:
            raise RuntimeError("protected event authority unavailable")
        project_id = access.identity["project_id"]
        async with self._seq_lock:
            project = self.get_or_create(project_id)
            project.seq = max(project.seq, authority.head(project_id),
                self.store.load_seq_watermark(project_id) if self.store is not None else 0)
            result = authority.commit(access, outbox_id, project.seq + 1)
            project.seq = max(project.seq, result.reference["seq"])
            if not result.created:
                return result  # Never insert an old sequence behind newer live events.
            try:
                if self.protected_enqueue_fault is not None:
                    self.protected_enqueue_fault()
                project.ring.append(result.reference)
                self._remember_idempotent_event(project_id, result.reference["event_id"], result.reference)
                for client in self.clients:
                    client.enqueue_ordered(result.reference)
            except BaseException:
                for client in self.clients:
                    client.request_resync(project_id, project.seq)
                raise
            if self.store is not None:
                # Derived ref failure does not invalidate the committed authority.
                self.store.defer_bg(lambda: self.store.append_event(project_id, result.reference),
                    description="protected event reference cache")
            return result

    def _with_group_identity(self, event: dict[str, Any]) -> dict[str, Any]:
        project_id = event.get("project_id")
        if not isinstance(project_id, str) or self.group_id_resolver is None:
            return event
        try:
            group_id = self.group_id_resolver(project_id)
        except Exception:
            group_id = None
        if not group_id:
            return event
        enriched = dict(event)
        enriched["group_id"] = group_id
        return enriched

    @staticmethod
    def _idempotency_payload(event: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in event.items()
            if key not in {"seq", "ts", "project_id", "project_name"}
        }

    def _find_idempotent_event(
        self,
        project_id: str,
        event_id: str,
        proj: Project,
    ) -> dict[str, Any] | None:
        cache = self._idempotent_events.setdefault(project_id, OrderedDict())
        if event_id in cache:
            cache.move_to_end(event_id)
            return dict(cache[event_id])
        for candidate in reversed(proj.ring.events()):
            if candidate.get("event_id") == event_id:
                self._remember_idempotent_event(project_id, event_id, candidate)
                return dict(candidate)
        if self.store is not None:
            try:
                durable = self.store.load_all_events(project_id)
            except Exception:
                durable = []
            for candidate in reversed(durable):
                if candidate.get("event_id") == event_id:
                    self._remember_idempotent_event(project_id, event_id, candidate)
                    return dict(candidate)
        return None

    def _remember_idempotent_event(
        self, project_id: str, event_id: str, event: dict[str, Any],
    ) -> None:
        """Bound the hot cache; ring/durable history remains the exact authority."""
        cache = self._idempotent_events.setdefault(project_id, OrderedDict())
        cache[event_id] = dict(event)
        cache.move_to_end(event_id)
        while len(cache) > self.IDEMPOTENCY_CACHE_MAX:
            cache.popitem(last=False)

    # ── 客户端 ──
    def add_client(self, client: Client) -> None:
        self.clients.add(client)

    def remove_client(self, client: Client) -> None:
        self.clients.discard(client)

    def prune(self) -> None:
        # WebSocket handler teardown owns recovery-debt transfer.  Health checks
        # must never erase a dead/fenced session from the minimum retirement
        # frontier before that handoff is durable.
        dead = {
            c for c in self.clients
            if not c.alive and c.task_runtime_recovery_retirement_complete
        }
        for c in dead:
            self.clients.discard(c)
        if dead:
            log.debug("pruned %d dead clients", len(dead))

    @property
    def client_count(self) -> int:
        return len([c for c in self.clients if c.alive])

    # ── 用户可见文本守卫 ──
    def set_public_text_filter(
        self, project_id: str, fn: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> None:
        """给项目登记最终出站过滤器。结构字段不动，只处理自然语言。"""
        self._public_text_filters[project_id] = fn

    def _filter_public_text(self, project_id: str, event: dict[str, Any]) -> dict[str, Any]:
        fn = self._public_text_filters.get(project_id)
        if fn is None:
            # 没有项目引擎（例如目录失效隔离期）也不能绕过硬脱敏。
            return sanitize_event(event, {})
        try:
            return fn(event)
        except Exception:
            # 注册过滤器若因花名册等外围状态出错，仍退回无状态硬过滤；绝不把原文直通。
            log.exception("[%s] 用户可见文本过滤失败，退回默认硬过滤", project_id)
            return sanitize_event(event, {})

    def clear_public_text_filter(self, project_id: str) -> None:
        """项目引擎关闭后解除 bound-method 引用，避免隔离期仍把整台旧引擎挂在内存里。"""
        self._public_text_filters.pop(project_id, None)

    # ── 项目 ──
    def get_or_create(self, project_id: str, name: str | None = None) -> Project:
        proj = self.projects.get(project_id)
        if proj is None:
            proj = Project(project_id, name or project_id, CONFIG.ring_capacity)
            if self.group_content is not None:
                proj.seq = max(proj.seq, self.group_content.head(project_id))
            if self.protected_events is not None:
                proj.seq = max(self.protected_events.head(project_id),
                    self.store.load_seq_watermark(project_id) if self.store is not None else 0)
            self.projects[project_id] = proj
            log.info("project created: %s (%s)", project_id, proj.name)
        elif name and proj.name == proj.id:
            proj.name = name
        return proj

    # ── [v0.4] 温载：把磁盘上的历史灌回内存 ──
    def restore(
        self, project_id: str, name: str, events: list[dict[str, Any]],
        seq_watermark: int = 0,
    ) -> Project:
        """
        从落盘的事件流水重建一个项目：ring 灌满、seq 接上。

        seq 必须接着最大的那个往下走——接错了，前端会把新事件当成重复的丢掉，
        界面就再也不动了。

        ★ [v0.12 D · 问题二] **ring 的容量按「这次要温载多少历史」现算**，
          至少 CONFIG.ring_capacity，装不下就撑大到刚好装得下（再留一截余量）。

          为什么要这样：前端重连时靠 ring 的增量回放拿历史（replay_since(0)）。
          ring 要是比历史小，最老那批就被挤掉了 → replay 报 gap → 前端只能拿到
          「最近一屏」，用户一看「我前面的聊天呢？」。历史一条没丢（磁盘上全在），
          但没喂进 ring 就等于没展示。
          现在落盘的只剩结构事件（小），把整段历史灌进 ring 内存完全扛得住。
          （运行期新来的瞬时事件会往 ring 里加，maxlen 兜底不让它无限涨；
            真被挤掉的也只是逐字增量，无所谓——磁盘那份才是聊天记录的真相。）
        """
        capacity = max(CONFIG.ring_capacity, len(events) + 256)
        proj = Project(project_id, name, capacity)
        for ev in events:
            proj.ring.append(ev)
        proj.seq = max(max((int(e["seq"]) for e in events), default=0), int(seq_watermark or 0))
        if self.group_content is not None:
            for reference in self.group_content.references(project_id):
                proj.ring.append(reference)
            proj.seq = max(proj.seq, self.group_content.head(project_id))
        if self.protected_events is not None:
            proj.seq = max(proj.seq, self.protected_events.head(project_id))
        # v1.0.35.3: 恢复已读水位（跨重启保留未读语义）。
        # 老数据无 .read 记录 → 历史算已读并立即落盘（last_read_seq = seq），
        # 避免升级后「全部历史变未读」吓到用户，同时让升级后的新消息能正确算未读。
        if self.store is not None:
            read = self.store.load_read_watermark(project_id)
            if read > 0:
                proj.last_read_seq = read
            else:
                proj.last_read_seq = proj.seq
                self.store.save_read_watermark(project_id, proj.seq)
        self.projects[project_id] = proj
        return proj

    def has(self, project_id: str) -> bool:
        return project_id in self.projects

    def remove_project(self, project_id: str) -> bool:
        """从实时 Hub 精确移除一个群或 DM，并清掉客户端宣告/过滤器引用。幂等。"""
        existed = self.projects.pop(project_id, None) is not None
        self._idempotent_events.pop(project_id, None)
        self.clear_public_text_filter(project_id)
        for client in self.clients:
            client.sent_projects.discard(project_id)
        return existed

    def remove_project_tree(self, project_id: str) -> int:
        """移除项目群及其全部 ``dm:{project}:*`` 实时频道。"""
        targets = [
            pid for pid in list(self.projects)
            if pid == project_id or pid.startswith(f"dm:{project_id}:")
        ]
        for pid in targets:
            self.remove_project(pid)
        return len(targets)

    def replay(
        self, project_id: str, since_seq: int,
    ) -> tuple[list[dict[str, Any]], bool, str]:
        """
        重连回放。Store 合并 JSONL 与 protected SQLite 真源，ring 只是增量层。

        · since_seq == 0：总是把磁盘全量结构历史与 ring 合并，保证 Electron 单独重开时
          初始消息/审批卡不会因运行期 ring 淘汰而消失。
        · ring 已有 gap：有落盘时用磁盘兜底，不把“磁盘完整、界面缺一块”推给前端。
        · 纯内存模式：仍保留原有 resync_required 语义。
        """
        proj = self.get_or_create(project_id)
        ring_events, gap = proj.ring.replay_since(since_seq)

        if since_seq > proj.seq:
            return [], True, "client-ahead"

        # Protected commit may survive a failed enqueue with no detectable ring gap.
        if self.store is not None and (since_seq == 0 or gap or self.protected_events is not None):
            durable = self.store.load_all_events(project_id)
            merged: dict[int, dict[str, Any]] = {}
            for ev in durable:
                seq = ev.get("seq")
                if isinstance(seq, int) and seq > since_seq:
                    merged[seq] = ev
            # 持久回放只重建结构状态。旧 stream_delta/tool_start 等瞬时帧若被重放，
            # 反而可能让刚重连的前端留下一个永远转圈的“正在输入”状态。
            # Ring 在这里仅补“已经形成结构结果、但磁盘写入偶发失败”的事件。
            for ev in proj.ring.structural(STRUCTURAL_EVENT_TYPES):
                seq = ev.get("seq")
                if isinstance(seq, int) and seq > since_seq:
                    merged[seq] = ev
            events = [self._filter_public_text(project_id, merged[k]) for k in sorted(merged)]
            return events, False, "disk+ring"

        events = [self._filter_public_text(project_id, ev) for ev in ring_events]
        return events, gap, "ring"

    async def replay_with_cursor(
        self, project_id: str, since_seq: int,
    ) -> tuple[list[dict[str, Any]], bool, str, int]:
        """Take the replay slice and its Hub high-water mark under the emit lock."""
        async with self._seq_lock:
            project = self.get_or_create(project_id)
            # A client watermark above this epoch cannot be used as a delta cursor.
            # Replay the complete new-epoch history so the subsequent lower
            # replay_complete cursor can rebuild conversation without another snapshot.
            effective_since = 0 if since_seq > project.seq else since_seq
            events, gap, source = self.replay(project_id, effective_since)
            cursor = project.seq
        return events, gap, source, cursor

    def durable_conversation(self, project_id: str) -> list[dict[str, Any]]:
        """快照的 conversation：磁盘全量结构历史 + ring 中尚未落盘的结构事件。"""
        proj = self.get_or_create(project_id)
        merged: dict[int, dict[str, Any]] = {}
        if self.group_content is not None:
            for reference in self.group_content.references(project_id):
                if reference.get("type") in STRUCTURAL_EVENT_TYPES:
                    merged[reference["seq"]] = reference
        if self.store is not None:
            for ev in self.store.load_all_events(project_id):
                seq = ev.get("seq")
                if isinstance(seq, int) and ev.get("type") in STRUCTURAL_EVENT_TYPES:
                    merged[seq] = ev
        for ev in proj.ring.structural(STRUCTURAL_EVENT_TYPES):
            seq = ev.get("seq")
            if isinstance(seq, int):
                merged[seq] = ev
        return [self._filter_public_text(project_id, merged[k]) for k in sorted(merged)]

    def durable_since(self, project_id: str, after_seq: int) -> list[dict[str, Any]]:
        """
        [v1.0.23.6] 增量读取：磁盘全量结构历史 + ring 未落盘结构事件，只留 seq > after_seq。

        与 durable_conversation 同源（同一合并逻辑），差异只在 seq 过滤——
        供 HTTP 旁路增量接口（GET /api/events）使用，前端启动预热「先有内容再等快照」。

        · 返回按 seq 升序、已脱敏（_filter_public_text）的结构事件；
        · after_seq 为 0 时与 durable_conversation 等价（全量）；
        · 空增量返回 []，前端据此判断「已同步到最新」。
        """
        if after_seq < 0:
            after_seq = 0
        proj = self.get_or_create(project_id)
        merged: dict[int, dict[str, Any]] = {}
        if self.store is not None:
            for ev in self.store.load_all_events(project_id):
                seq = ev.get("seq")
                if isinstance(seq, int) and seq > after_seq \
                        and ev.get("type") in STRUCTURAL_EVENT_TYPES:
                    merged[seq] = ev
        for ev in proj.ring.structural(STRUCTURAL_EVENT_TYPES):
            seq = ev.get("seq")
            if isinstance(seq, int) and seq > after_seq:
                merged[seq] = ev
        if self.group_content is not None:
            for ev in self.group_content.references(project_id):
                seq = ev.get("seq")
                if isinstance(seq, int) and seq > after_seq and ev.get("type") in STRUCTURAL_EVENT_TYPES:
                    merged[seq] = ev
        return [self._filter_public_text(project_id, merged[k]) for k in sorted(merged)]

    # ── 发事件：引擎级（盖 seq、进 ring、广播） ──
    async def emit(self, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        event, _created = await self.emit_with_status(project_id, payload)
        return event

    async def emit_with_status(
        self, project_id: str, payload: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        """
        引擎级事件的唯一出口：
          注入 project_id / project_name / ts → 锁内盖 seq → 进 ring → 校验 → 广播。

        The status bit is false when ``event_id`` resolves to an existing durable
        envelope.  Recovery publishers use it to avoid rebroadcasting an already-live
        RunEvent after their bounded cursor cache has evicted the run.
        """
        from .protected_events import REF
        from .protected_storage import ProtectedStorageError, protected_id
        if (REF in payload or protected_id(payload.get("task_id"))
                or str(payload.get("event_id", "")).startswith("p2evt_")):
            raise ProtectedStorageError("protected events require explicit backend publication")
        etype = payload.get("type")
        if etype in NO_SEQ_EVENT_TYPES:
            raise ContractViolation(f"{etype} 属无 seq 白名单，不能走 emit()（会被盖上 seq）")

        proj = self.get_or_create(project_id)

        event = dict(payload)
        event["project_id"] = project_id
        event["ts"] = now_ts()
        # user_echo 的契约里没有 project_name 字段（envelope.ts 逐字段核对过）——不能塞
        if etype != "user_echo":
            event["project_name"] = proj.name
        event = self._with_group_identity(event)
        event = self._filter_public_text(project_id, event)

        async with self._seq_lock:          # ★ 单点盖号：跨项目也串行，杜绝竞态
            event_id = event.get("event_id")
            if isinstance(event_id, str) and event_id:
                existing = self._find_idempotent_event(project_id, event_id, proj)
                if existing is not None:
                    from .task_runtime.group_content import REF as CHAT_REF
                    if CHAT_REF in existing and self.group_content is not None:
                        existing = self.group_content.hydrate(existing)
                    if self._idempotency_payload(existing) != self._idempotency_payload(event):
                        # Completion-triggered coordinator turns can be replayed after a
                        # process dies between user-message persistence and notification
                        # acknowledgement.  The first durable response is authoritative;
                        # a nondeterministic model rerun must not create a second user
                        # bubble or replace that response.
                        if event_id.startswith("coordmsg_"):
                            return existing, False
                        raise ContractViolation(
                            f"event_id {event_id!r} 已用于不同 payload，拒绝覆盖"
                        )
                    return existing, False
            proj.seq += 1
            event["seq"] = proj.seq
            transient_event = event
            if self.group_content is not None:
                event = self.group_content.seal_event(event)
            proj.ring.append(event)
            if self.store is not None:
                # 结构事件落盘：[v1.0.24.4] 提交进持久化队列即返回，磁盘写入在后台
                # 单线程上跑。提交发生在 seq 锁内 → 入队顺序 = seq 顺序 → 盘上不乱序。
                _store, _pid, _event, _seq = self.store, project_id, event, proj.seq
                _store.defer_bg(lambda: _store.append_event(_pid, _event),
                                description=f"{_pid} append_event")
                _store.defer_bg(lambda: _store.save_seq_watermark(_pid, _seq),
                                description=f"{_pid} seq 高水位")
            if isinstance(event_id, str) and event_id:
                self._remember_idempotent_event(project_id, event_id, event)

            if self.protected_events is not None:
                for client in self.clients:
                    client.enqueue_ordered(event)

        if self.protected_events is None:
            await self.broadcast(event)
        return transient_event, True

    # ── 发事件：服务器级（无 seq，旁路） ──
    async def emit_no_seq(self, payload: dict[str, Any]) -> dict[str, Any]:
        from .protected_events import REF
        from .protected_storage import ProtectedStorageError, protected_id
        if REF in payload or protected_id(payload.get("task_id")):
            raise ProtectedStorageError("protected events require explicit backend publication")
        event = self._with_group_identity(dict(payload))
        pid = event.get("project_id")
        if isinstance(pid, str):
            event = self._filter_public_text(pid, event)
        await self.broadcast(event)
        return event

    async def send_to(self, client: Client, payload: dict[str, Any]) -> bool:
        """点对点（握手回放、pong）。不进 ring、不广播。"""
        event = self._with_group_identity(dict(payload))
        pid = event.get("project_id")
        if isinstance(pid, str):
            event = self._filter_public_text(pid, event)
        else:
            event = sanitize_event(event, {})
        return await client.send(event)

    # ── 广播 ──
    async def broadcast(self, event: dict[str, Any], targets: Iterable[Client] | None = None) -> None:
        clients = list(targets if targets is not None else self.clients)
        if not clients:
            return
        results = await asyncio.gather(
            *(c.send_live(event) for c in clients), return_exceptions=True
        )
        for client, ok in zip(clients, results):
            if ok is not True:
                # The connection handler's ``finally`` block performs the durable
                # recovery-debt transfer.  Removing here would create a window in
                # which retirement can delete state the disconnected renderer has
                # not acknowledged.
                log.debug("client %s live send failed; awaiting owned teardown", client.id)

        # project_created 广播后，记账到每个活着的客户端（避免握手时重复补发）
        if event.get("type") == "project_created":
            pid = event["project_id"]
            for c in clients:
                if c.alive:
                    c.sent_projects.add(pid)

    # ── 快照 ──
    async def snapshot(
        self, project_id: str,
        activity: list[dict[str, Any]] | None = None,
        task_runtime: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        state_snapshot：**本身消耗一个 seq 并写入 ring**（PROTOCOL.md §e）。
        conversation 只放结构事件（stream_delta 这类瞬时事件不进时间线）。

        [v1.0.24.4] activity = 引擎权威活动账本全量条目（见 engine.open_activity_snapshot）。
        传 None = 不带（调用方拿不到引擎）；传 [] = 明确告知「现场无人在干活」。
        """
        proj = self.get_or_create(project_id)

        async with self._seq_lock:
            # conversation 与 last_seq 必须在同一把 seq 锁下取，避免并发 emit 落在两者之间，
            # 造成“last_seq 已包含某条消息、快照正文却漏了它”的不一致快照。
            conversation = self.durable_conversation(project_id)
            last_seq = proj.seq
            proj.seq += 1
            event = {
                "type": "state_snapshot",
                "project_id": project_id,
                "last_seq": last_seq,
                "agents": list(proj.members),
                "conversation": conversation,
                "pending_card": proj.pending_card,
                "unread_count": self._count_unread(proj.last_read_seq, conversation),
                "ts": now_ts(),
                "seq": proj.seq,
            }
            if activity is not None:
                event["activity"] = list(activity)
            if task_runtime is not None:
                event["task_runtime"] = dict(task_runtime)
            event = self._with_group_identity(event)
            event = self._filter_public_text(project_id, event)
            proj.ring.append(event)
            if self.store is not None:
                # state_snapshot 本身不落聊天流水，但 seq 高水位必须持久化。
                # [v1.0.24.4] 同 emit：入队即返回，写盘在后台单线程（顺序由队列保证）。
                _store, _pid, _event, _seq = self.store, project_id, event, proj.seq
                _store.defer_bg(lambda: _store.append_event(_pid, _event),
                                description=f"{_pid} snapshot append_event")
                _store.defer_bg(lambda: _store.save_seq_watermark(_pid, _seq),
                                description=f"{_pid} snapshot seq 高水位")

            if self.protected_events is not None:
                for client in self.clients:
                    client.enqueue_ordered(event)

        if self.protected_events is None:
            await self.broadcast(event)
        return event

    # ── 已读水位 ──
    def unread_count(self, project_id: str) -> int:
        """未读消息数 = 已读水位之后的消息/审批卡数（与前端 isUnreadEvent 对齐）。

        v1.0.35.3 之前用 seq 事件数（未读虚高，如 186 vs 实际 6 条），现改为数
        durable_conversation 里的 message / approval_card。读盘发生在握手/快照路径，
        非逐消息路径，可接受。
        """
        proj = self.projects.get(project_id)
        if proj is None:
            return 0
        return self._count_unread(proj.last_read_seq, self.durable_conversation(project_id))

    @staticmethod
    def _count_unread(last_read_seq: int, conversation: list[dict[str, Any]]) -> int:
        return sum(
            1 for ev in conversation
            if ev.get("seq", 0) > last_read_seq and ev.get("type") in UNREAD_EVENT_TYPES
        )

    def mark_read(self, project_id: str, seq: int) -> None:
        proj = self.projects.get(project_id)
        if proj is not None:
            proj.last_read_seq = max(proj.last_read_seq, seq)
            # v1.0.35.3: 落盘，否则重启归零、全部历史被算成未读。
            if self.store is not None:
                _pid, _seq = project_id, proj.last_read_seq
                self.store.defer_bg(lambda: self.store.save_read_watermark(_pid, _seq),
                                    description=f"{_pid} 已读水位")

    # ── /health ──
    def health(self) -> dict[str, Any]:
        self.prune()
        return {
            "status": "ok",
            "project_count": len(self.projects),
            "ws_clients": self.client_count,
        }

