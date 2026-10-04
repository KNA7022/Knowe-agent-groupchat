"""Private approval state; never pass these objects to public serializers."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hmac
import json
import re
import sqlite3
import sys
from typing import Any, Literal

from backend.knowe_rag.crypto import MasterKey, MasterKeyError, field_aad, keyed_content_hasher

from .contracts import opaque_id

_KEY_SENTINEL = b"studycrew.card-key.v1"


class CardStateError(RuntimeError):
    """Content-free denial at the private card boundary."""


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def bounded(value: str, limit: int, *, empty: bool = False) -> None:
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value) > limit:
        raise CardStateError("invalid bounded card field")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise CardStateError("invalid UTF-8 card field") from None


def request_id(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", value) is None:
        raise CardStateError("invalid opaque card request key")


@dataclass(frozen=True, repr=False)
class CardProposal:
    target_role: str
    instruction: str
    title: str = ""
    description: str = ""
    target_agent_id: str = ""

    def __post_init__(self):
        if not isinstance(self.target_role, str) or self.target_role not in {"knowledge", "planning", "coach", "expert", "legacy_expert"}:
            raise CardStateError("invalid card target role")
        bounded(self.instruction, 16_384)
        bounded(self.title, 256, empty=True)
        bounded(self.description, 2048, empty=True)
        if not isinstance(self.target_agent_id, str) or (self.target_agent_id and
            re.fullmatch(r"gag_[0-9a-f]{32}", self.target_agent_id) is None):
            raise CardStateError("invalid card target identity")

    @classmethod
    def from_payload(cls, raw: dict) -> CardProposal:
        if not isinstance(raw, dict) or set(raw) - set(cls.__dataclass_fields__):
            raise CardStateError("reserved or unknown card proposal field")
        try:
            return cls(**raw)
        except (TypeError, ValueError):
            raise CardStateError("invalid card proposal") from None


@dataclass(frozen=True)
class CardReceipt:
    request_key: str
    request_digest: str
    action: Literal["created", "amended", "approved", "rejected"]
    generation: int

    def __post_init__(self):
        request_id(self.request_key)
        if self.action not in {"created", "amended", "approved", "rejected"}:
            raise CardStateError("invalid card lifecycle receipt")
        if re.fullmatch(r"[0-9a-f]{64}", self.request_digest) is None:
            raise CardStateError("invalid card receipt digest")
        if type(self.generation) is not int or self.generation < 1:
            raise CardStateError("invalid card receipt generation")


@dataclass(frozen=True, repr=False)
class BoundCardState:
    approval_id: str
    owner_task_id: str
    proposal: CardProposal
    proposal_digest: str
    scope_revision: str
    expires_at: str
    content_generation: int = 1
    state_version: int = 1
    feedback_epoch: int = 0
    receipts: tuple[CardReceipt, ...] = field(default_factory=tuple)

    def __post_init__(self):
        if re.fullmatch(r"apr_[0-9a-f]{32}", self.approval_id) is None or re.fullmatch(
            r"task_[0-9a-f]{32}", self.owner_task_id
        ) is None:
            raise CardStateError("invalid private card identity")
        for name, minimum in (("content_generation", 1), ("state_version", 1), ("feedback_epoch", 0)):
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= 2**53 - 1:
                raise CardStateError("invalid card version")
        if not 1 <= len(self.receipts) <= 64 or len({r.request_key for r in self.receipts}) != len(self.receipts):
            raise CardStateError("card request receipt capacity exceeded")
        if not isinstance(self.proposal, CardProposal) or not self.proposal.target_agent_id:
            raise CardStateError("card target must be canonically bound")
        for digest in (self.proposal_digest, self.scope_revision):
            if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                raise CardStateError("invalid private card digest")


class CardStateRepository:
    """Encryption capability on the existing runtime database, no independent DB."""

    def __init__(self, db, master_key: MasterKey | None):
        self.db = db
        # Existing RAG and memory imports support both package spellings. Accept
        # only their actual MasterKey classes, never a renderer-shaped duck type.
        alias = sys.modules.get("knowe_rag.crypto")
        alias_type = getattr(alias, "MasterKey", None)
        if isinstance(alias_type, type) and isinstance(master_key, alias_type):
            master_key = MasterKey(master_key.key_material)
        self._key = master_key
        db.ensure_columns("studycrew_approvals", {
            "card_state_ciphertext": "BLOB", "card_crypto_version": "INTEGER",
            "card_key_id": "TEXT", "card_state_version": "INTEGER",
            "card_generation": "INTEGER", "card_epoch": "INTEGER",
            "card_invalidated": "TEXT", "card_receipts_json": "TEXT",
            "card_receipts_mac": "TEXT",
        })
        db.initialize("""
            PRAGMA secure_delete=ON;
            CREATE TABLE IF NOT EXISTS studycrew_card_crypto (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                store_id TEXT NOT NULL, key_id TEXT NOT NULL, sentinel BLOB NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS studycrew_card_owner_terminal
            AFTER UPDATE OF status ON studycrew_task_runs
            WHEN NEW.status IN ('succeeded','partial','failed','cancelled')
            BEGIN
                UPDATE studycrew_approvals SET card_state_ciphertext=NULL,
                    card_invalidated='owner_terminal'
                    WHERE run_id=NEW.run_id AND card_key_id IS NOT NULL;
                DELETE FROM studycrew_permission_grants WHERE approval_id IN (
                    SELECT approval_id FROM studycrew_approvals
                    WHERE run_id=NEW.run_id AND card_key_id IS NOT NULL);
            END;
            CREATE TRIGGER IF NOT EXISTS studycrew_card_rejected
            AFTER UPDATE OF status ON studycrew_approvals WHEN NEW.status='rejected'
            BEGIN
                UPDATE studycrew_approvals SET card_state_ciphertext=NULL,
                    card_invalidated='rejected'
                    WHERE approval_id=NEW.approval_id AND card_key_id IS NOT NULL;
                DELETE FROM studycrew_permission_grants WHERE approval_id=NEW.approval_id;
            END;
        """)

    def digest(self, domain: str, value: Any) -> str:
        if not isinstance(self._key, MasterKey):
            raise CardStateError("card encryption capability unavailable")
        return keyed_content_hasher(self._key)(canonical(["studycrew.card", 1, domain, value]))

    def verify(self, conn, *, initialize: bool = False):
        if not isinstance(self._key, MasterKey):
            raise CardStateError("card encryption capability unavailable")
        row = conn.execute("SELECT * FROM studycrew_card_crypto WHERE singleton=1").fetchone()
        if row is None:
            occupied = conn.execute("SELECT 1 FROM studycrew_approvals WHERE card_key_id IS NOT NULL "
                                    "OR card_state_ciphertext IS NOT NULL OR card_crypto_version IS NOT NULL "
                                    "OR card_state_version IS NOT NULL LIMIT 1").fetchone()
            if occupied or not initialize:
                raise CardStateError("card key sentinel unavailable")
            store_id, key_id = opaque_id("store"), opaque_id("key")
            aad = field_aad(canonical(["studycrew.card-key", 1, store_id, key_id]), "sentinel")
            conn.execute("INSERT INTO studycrew_card_crypto VALUES (1,?,?,?)",
                         (store_id, key_id, self._key.seal(_KEY_SENTINEL, aad=aad)))
            row = conn.execute("SELECT * FROM studycrew_card_crypto WHERE singleton=1").fetchone()
        try:
            if not isinstance(row["sentinel"], bytes) or len(row["sentinel"]) != len(_KEY_SENTINEL) + 28:
                raise CardStateError("invalid card key sentinel envelope")
            aad = field_aad(canonical(["studycrew.card-key", 1, row["store_id"], row["key_id"]]), "sentinel")
            if self._key.open(row["sentinel"], aad=aad) != _KEY_SENTINEL:
                raise CardStateError("card key sentinel invalid")
        except (MasterKeyError, TypeError, ValueError):
            raise CardStateError("card key sentinel authentication failed") from None
        return row

    @staticmethod
    def _aad(meta, row, task_id: str) -> bytes:
        return field_aad(canonical(["studycrew.card-state", 1, meta["store_id"], row["card_key_id"],
            row["group_id"], task_id, row["run_id"], row["approval_id"], row["card_generation"],
            row["card_epoch"], row["card_state_version"]]), "studycrew_approvals.card_state")

    def open(self, conn, row, task_id: str) -> BoundCardState:
        meta = self.verify(conn)
        if row["card_state_ciphertext"] is None or row["card_invalidated"] is not None:
            raise CardStateError("card is unbound or invalidated")
        if row["card_crypto_version"] != 1 or row["card_key_id"] != meta["key_id"]:
            raise CardStateError("unsupported card encryption envelope")
        try:
            blob = row["card_state_ciphertext"]
            if not isinstance(blob, bytes) or not 28 <= len(blob) <= 128 * 1024:
                raise CardStateError("card encryption envelope exceeds limit")
            plaintext = self._key.open(blob, aad=self._aad(meta, row, task_id))
            raw = json.loads(plaintext.decode("utf-8", errors="strict"))
            if canonical(raw).encode("utf-8") != plaintext:
                raise CardStateError("noncanonical private card state")
            raw["proposal"] = CardProposal.from_payload(raw["proposal"])
            raw["receipts"] = tuple(CardReceipt(**item) for item in raw["receipts"])
            state = BoundCardState(**raw)
            if (state.approval_id, state.owner_task_id, state.content_generation, state.state_version,
                state.feedback_epoch) != (row["approval_id"], task_id, row["card_generation"],
                                         row["card_state_version"], row["card_epoch"]):
                raise CardStateError("card identity/version mismatch")
            if not hmac.compare_digest(state.proposal_digest, self.digest("proposal", asdict(state.proposal))):
                raise CardStateError("card proposal digest mismatch")
            return state
        except (MasterKeyError, UnicodeError, ValueError, TypeError, KeyError):
            raise CardStateError("card state authentication failed") from None

    def write(self, conn, row, state: BoundCardState) -> None:
        meta = self.verify(conn)
        envelope = dict(row) | {"card_key_id": meta["key_id"], "card_generation": state.content_generation,
                               "card_state_version": state.state_version, "card_epoch": state.feedback_epoch}
        sealed = self._key.seal(canonical(asdict(state)).encode("utf-8"),
                                aad=self._aad(meta, envelope, state.owner_task_id))
        receipts = canonical([asdict(receipt) for receipt in state.receipts])
        receipt_mac = self.digest("receipts", [row["approval_id"], row["group_id"], row["run_id"], receipts])
        conn.execute("""UPDATE studycrew_approvals SET card_state_ciphertext=?, card_crypto_version=1,
            card_key_id=?, card_state_version=?, card_generation=?, card_epoch=?, card_invalidated=NULL,
            card_receipts_json=?, card_receipts_mac=? WHERE approval_id=?""",
            (sealed, meta["key_id"], state.state_version, state.content_generation, state.feedback_epoch,
             receipts, receipt_mac, state.approval_id))

    def terminal_receipts(self, conn, row) -> tuple[CardReceipt, ...]:
        self.verify(conn)
        raw = row["card_receipts_json"]
        digest = self.digest("receipts", [row["approval_id"], row["group_id"], row["run_id"], raw])
        if not isinstance(row["card_receipts_mac"], str) or not hmac.compare_digest(digest, row["card_receipts_mac"]):
            raise CardStateError("card terminal receipt authentication failed")
        try:
            receipts = tuple(CardReceipt(**item) for item in json.loads(raw))
            if len(receipts) > 64:
                raise CardStateError("card receipt capacity exceeded")
            return receipts
        except (ValueError, TypeError):
            raise CardStateError("invalid terminal card receipts") from None

    @staticmethod
    def invalidate(conn, approval_id: str, reason: str):
        conn.execute("UPDATE studycrew_approvals SET card_state_ciphertext=NULL, card_invalidated=? "
                     "WHERE approval_id=? AND card_key_id IS NOT NULL", (reason, approval_id))
        conn.execute("DELETE FROM studycrew_permission_grants WHERE approval_id=?", (approval_id,))

    def checkpoint(self) -> None:
        """Discard superseded ciphertext from WAL after a revocation commit."""
        self.db.initialize("PRAGMA wal_checkpoint(TRUNCATE);")


STORAGE_FAILURES = (sqlite3.Error, MasterKeyError, OSError)
