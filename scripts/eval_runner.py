"""Versioned evaluation runner. No credentials, implicit network, or model defaults."""
from __future__ import annotations
import argparse, hashlib, importlib, json, os, sys, time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))


def load_cases(path):
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = set()
    for row in rows:
        if row["id"] in ids or row["split"] not in ("development", "holdout"):
            raise ValueError("Duplicate id or invalid split")
        ids.add(row["id"])
        if not row.get("expected") or not row.get("steps") or row["version"] != 1:
            raise ValueError("Missing evaluation annotation")
        checks = [e["check"] for e in row["expected"]]
        if len(checks) != len(set(checks)) or any("equals" not in e for e in row["expected"]):
            raise ValueError("Expected checks need unique names and explicit oracles")
    manifest_path = Path(path).with_suffix(".manifest.json")
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != manifest["sha256"] or len(rows) != manifest["cases"]:
            raise ValueError("Fixed dataset does not match its version manifest")
    return rows


class Ledger:
    """Single authoritative task ledger. Uncertain attempts retain full reservation.

    Exclusive run lock intentionally requires manual inspection after a crash;
    deleting a lock without inspecting outstanding reservations is unsafe.
    """
    def __init__(self, path, ceiling):
        self.path = Path(path)
        self.ceiling = Decimal(str(ceiling))
        if not self.ceiling.is_finite() or not 0 < self.ceiling <= 50:
            raise ValueError("Budget must be positive and <= CNY 50")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = self.path.with_suffix(self.path.suffix + ".lock")
        self.fd = os.open(self.lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(self.fd, str(os.getpid()).encode())
        try:
            self.events = [json.loads(s) for s in self.path.read_text(encoding="utf-8").splitlines()] if self.path.exists() else []
            reserved = {e["ticket"] for e in self.events if e["kind"] == "reserve"}
            settled = {e["ticket"] for e in self.events if e["kind"] in {"settle", "rate_limit_retained"}}
            if reserved - settled:
                raise RuntimeError("Uncertain earlier paid attempt: reconcile provider bill before more calls")
        except Exception:
            self.close()
            raise
    def close(self):
        os.close(self.fd)
        self.lock.unlink()
    def append(self, event):
        with self.path.open("a", encoding="utf-8") as out:
            out.write(json.dumps(event, ensure_ascii=False) + "\n")
            out.flush()
            os.fsync(out.fileno())
        self.events.append(event)
    def spent(self):
        return sum((Decimal(e["cny"]) for e in self.events), Decimal(0))
    def reserve(self, case_id, amount):
        if not amount.is_finite() or amount <= 0:
            raise ValueError("Invalid cost reservation")
        if self.spent() + amount > self.ceiling:
            raise RuntimeError("Budget exhausted; request was NOT sent")
        ticket = str(time.time_ns())
        self.append(dict(ticket=ticket, case_id=case_id, kind="reserve", cny=str(amount)))
        return ticket
    def settle(self, ticket, reserved, actual):
        if not actual.is_finite() or actual < 0 or actual > reserved:
            raise RuntimeError("Usage exceeds reservation: stop and audit provider billing")
        self.append(dict(ticket=ticket, kind="settle", cny=str(actual-reserved)))


def summarize(output, identity):
    records = [json.loads(s) for s in output.read_text(encoding="utf-8").splitlines()] if output.exists() else []
    latest = {r["id"]: r for r in records if r.get("run_identity") == identity}
    counts = {status: sum(r["status"] == status for r in latest.values()) for status in ("passed", "failed", "error", "blocked")}
    summary = dict(run_identity=identity, cases=len(latest), counts=counts,
                   categories={c: {status: sum(r["category"] == c and r["status"] == status for r in latest.values()) for status in counts} for c in sorted({r["category"] for r in latest.values()})},
                   failures=[{k:r[k] for k in ("id", "category", "status", "checks", "error_type", "blocked", "evidence") if k in r} for r in latest.values() if r["status"] != "passed"],
                   live_product_e2e=False)
    output.with_suffix(".summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(dict(summary=str(output.with_suffix('.summary.json')), **counts)))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", default=str(Path(__file__).resolve().parents[1]/"evals/v1.jsonl"))
    ap.add_argument("--mode", choices=("validate", "offline", "live", "product-probe", "protocol-probe", "rag-probe", "ui-probe", "memory-probe"), default="validate")
    ap.add_argument("--build-runtime", action="store_true", help="Use the project's pinned Python build environment")
    ap.add_argument("--adapter", help="module:function; live uses only evals.adapters:live")
    ap.add_argument("--pricing", help="Verified model pricing JSON; mandatory for live")
    ap.add_argument("--ledger", help="Shared ledger for ALL calls in this task, mandatory for live")
    ap.add_argument("--output", default="evals/results.jsonl")
    ap.add_argument("--split", choices=("all", "development", "holdout"), default="all")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--ids", help="Comma-separated case IDs for a small pilot")
    ap.add_argument("--retries", type=int, default=0)
    ap.add_argument("--rpm", type=float, default=6)
    args = ap.parse_args()
    if args.build_runtime:
        import subprocess
        python = ROOT/'.venv-build'/'Scripts'/'python.exe'
        if not python.is_file():
            raise RuntimeError('Pinned build runtime is not installed')
        raise SystemExit(subprocess.call([str(python),str(Path(__file__).resolve()),
                                         *[a for a in sys.argv[1:] if a!='--build-runtime']]))
    if args.mode in {"product-probe", "protocol-probe", "rag-probe", "ui-probe", "memory-probe"}:
        from budget_product_probe import main as product_main
        product_main(args, Ledger)
        return
    rows = load_cases(args.dataset)
    if args.mode == "validate":
        print(json.dumps(dict(dataset_valid=True, cases=len(rows), product_execution=False, live_execution=False)))
        return
    args.adapter = args.adapter or ("evals.adapters:live" if args.mode == "live" else "evals.adapters:local")
    if args.rpm <= 0 or not 0 <= args.retries <= 3:
        ap.error("adapter, positive rpm and retries 0..3 required")
    mod, name = args.adapter.split(":", 1)
    adapter = getattr(importlib.import_module(mod), name)
    pricing = {}
    ledger = None
    if args.mode == "live":
        if args.adapter != "evals.adapters:live":
            ap.error("Live execution must use the bounded built-in adapter")
        if not args.pricing or not args.ledger:
            ap.error("live requires verified pricing and shared ledger")
        pricing = json.loads(Path(args.pricing).read_text(encoding="utf-8"))
        for key in ("model", "provider", "endpoint", "transport", "official_source", "verified_at", "input_cny_per_million", "output_cny_per_million", "max_input_tokens", "max_output_tokens", "budget_cny", "text_token_bound"):
            if key not in pricing:
                raise ValueError("Missing verified price field: " + key)
        if not pricing.get("verified") or pricing.get("currency") != "CNY":
            raise ValueError("Unverified pricing/currency")
        if any(not Decimal(str(pricing[k])).is_finite() or Decimal(str(pricing[k])) <= 0 for k in ("input_cny_per_million", "output_cny_per_million")) or any(type(pricing[k]) is not int or pricing[k] <= 0 for k in ("max_input_tokens", "max_output_tokens")):
            raise ValueError("Invalid price/token limits")
    fingerprint = hashlib.sha256(Path(args.dataset).read_bytes()).hexdigest()
    implementation = hashlib.sha256(Path(__file__).read_bytes() + (ROOT/"evals/adapters.py").read_bytes())
    for source in sorted((ROOT/"backend").rglob("*.py")):
        if "tests" not in source.parts and "__pycache__" not in source.parts:
            implementation.update(source.relative_to(ROOT).as_posix().encode())
            implementation.update(source.read_bytes())
    code_hash = implementation.hexdigest()
    identity = hashlib.sha256(json.dumps(dict(dataset=fingerprint, mode=args.mode, adapter=args.adapter, pricing=pricing, code=code_hash), sort_keys=True).encode()).hexdigest()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    prior = [json.loads(s) for s in output.read_text(encoding="utf-8").splitlines()] if output.exists() else []
    done = {r["id"] for r in prior if r.get("run_identity") == identity and r.get("status") == "passed"}
    selected = [r for r in rows if (args.split == "all" or r["split"] == args.split) and r["id"] not in done]
    if args.ids:
        requested = set(args.ids.split(","))
        if requested - {r["id"] for r in rows}:
            raise ValueError("Unknown pilot case ID")
        selected = [r for r in selected if r["id"] in requested]
    if args.limit is not None:
        if args.limit <= 0:
            raise ValueError("limit must be positive")
        selected = selected[:args.limit]
    if args.mode == "live":
        ledger = Ledger(args.ledger, pricing["budget_cny"])
    try:
        for case in selected:
            for attempt in range(args.retries + 1):
                reserved = Decimal(0)
                ticket = None
                if ledger:
                    reserved = (Decimal(str(pricing["input_cny_per_million"]))*int(pricing["max_input_tokens"]) + Decimal(str(pricing["output_cny_per_million"]))*int(pricing["max_output_tokens"]))/1000000
                    ticket = ledger.reserve(case["id"], reserved)
                record = dict(id=case["id"], category=case["category"], split=case["split"], mode=args.mode, run_identity=identity, attempt=attempt, status="error", time=time.time())
                try:
                    # Adapter MUST enforce token caps before a single request, no nested retries.
                    result = adapter(case, dict(pricing, mode=args.mode))
                    if result.get("blocked"):
                        if ledger and not result.get("not_sent"):
                            raise RuntimeError("Uncertain blocked attempt")
                        if ledger:
                            ledger.settle(ticket, reserved, Decimal(0))
                        record.update(status="blocked", blocked=result["blocked"], evidence=result.get("evidence", {}))
                        with output.open("a", encoding="utf-8") as out:
                            out.write(json.dumps(record, ensure_ascii=False)+"\n")
                            out.flush()
                            os.fsync(out.fileno())
                        break
                    if ledger:
                        usage = result["usage"]
                        for k, cap in (("input_tokens", "max_input_tokens"), ("output_tokens", "max_output_tokens")):
                            if type(usage[k]) is not int or not 0 <= usage[k] <= int(pricing[cap]):
                                raise RuntimeError("Invalid usage; reservation retained")
                        actual = (Decimal(usage["input_tokens"])*Decimal(str(pricing["input_cny_per_million"])) + Decimal(usage["output_tokens"])*Decimal(str(pricing["output_cny_per_million"])))/1000000
                        allowed = {pricing["model"], *pricing.get("allowed_response_model_ids", [])}
                        if result.get("model") not in allowed:
                            raise RuntimeError("Returned model mismatch; reservation retained")
                        ledger.settle(ticket, reserved, actual)
                        record.update(usage=usage, cny=str(actual), model=result["model"])
                    checks = result["checks"]
                    expected = {e["check"] for e in case["expected"]}
                    if set(checks) != expected or any(type(v) is not bool for v in checks.values()):
                        raise ValueError("Adapter must provide every annotated check as bool")
                    record.update(status="passed" if all(checks.values()) else "failed", checks=checks, evidence=result.get("evidence", {}))
                except Exception as exc:
                    # Do not persist arbitrary exception messages: providers can embed secrets.
                    record["error_type"] = type(exc).__name__
                    if ledger and type(exc).__name__ == "SafeRateLimit":
                        # Never assume rejection was free: retain its full reservation.
                        ledger.append(dict(ticket=ticket, kind="rate_limit_retained", cny="0"))
                with output.open("a", encoding="utf-8") as out:
                    out.write(json.dumps(record, ensure_ascii=False) + "\n")
                    out.flush()
                    os.fsync(out.fileno())
                print(json.dumps({k: record[k] for k in ("id", "status", "mode")}))
                if args.mode == "live":
                    time.sleep(min(60, 60 / args.rpm) * (2 ** attempt if record.get("error_type") == "SafeRateLimit" else 1))
                if record["status"] != "error":
                    break
                # Unknown live outcomes require inspection, never blind paid retries.
                if ledger and record.get("error_type") != "SafeRateLimit":
                    raise RuntimeError("Live request uncertain; stopped, full reservation retained")
    finally:
        if ledger:
            ledger.close()
        summarize(output, identity)

if __name__ == "__main__":
    main()
