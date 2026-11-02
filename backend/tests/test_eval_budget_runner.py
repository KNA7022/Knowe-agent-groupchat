"""No-network invariants for the shared paid-call budget and fixed evaluation set."""
from decimal import Decimal
from pathlib import Path
import importlib.util
import json

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("studycrew_eval_runner", ROOT/"scripts/eval_runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_fixed_corpus_has_independent_splits_and_pixel_inputs():
    rows = runner.load_cases(ROOT/"evals/v1.jsonl")
    assert len(rows) == 120
    assert sum(row["split"] == "holdout" for row in rows) == 30
    assert len({json.dumps([row["prompt"], row.get("image_sha256"), row["data"]], sort_keys=True) for row in rows}) == 120
    assert sum(bool(row.get("image_sha256")) for row in rows) == 24


def test_shared_budget_blocks_next_call_before_side_effect_and_persists(tmp_path):
    path = tmp_path/"ledger.jsonl"
    ledger = runner.Ledger(path, 50)
    ticket = ledger.reserve("pilot", Decimal("49.9"))
    with pytest.raises(RuntimeError, match="NOT sent"):
        ledger.reserve("next", Decimal("0.2"))
    ledger.settle(ticket, Decimal("49.9"), Decimal("49.8"))
    ledger.close()
    reopened = runner.Ledger(path, 50)
    assert reopened.spent() == Decimal("49.8")
    with pytest.raises(RuntimeError, match="NOT sent"):
        reopened.reserve("next", Decimal("0.3"))
    reopened.close()


def test_uncertain_attempt_blocks_resume_without_forgetting_cost(tmp_path):
    path = tmp_path/"ledger.jsonl"
    ledger = runner.Ledger(path, 50)
    ledger.reserve("timeout", Decimal("0.17"))
    ledger.close()
    with pytest.raises(RuntimeError, match="Uncertain earlier"):
        runner.Ledger(path, 50)
    assert not path.with_suffix(".jsonl.lock").exists()
    assert json.loads(path.read_text(encoding="utf-8"))["cny"] == "0.17"


def test_concurrent_runner_cannot_open_same_ledger(tmp_path):
    path = tmp_path/"ledger.jsonl"
    ledger = runner.Ledger(path, 50)
    try:
        with pytest.raises(FileExistsError):
            runner.Ledger(path, 50)
    finally:
        ledger.close()


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "51", "0", "-1"])
def test_invalid_budget_never_creates_a_ledger(tmp_path, amount):
    with pytest.raises(ValueError):
        runner.Ledger(tmp_path/"ledger.jsonl", amount)
    assert not list(tmp_path.iterdir())


def test_image_hash_and_dataset_hash_prevent_silent_fixture_replacement(tmp_path):
    source = ROOT/"evals/v1.jsonl"
    target = tmp_path/"v1.jsonl"
    target.write_text(source.read_text(encoding="utf-8").replace("3x²", "wrong", 1), encoding="utf-8")
    (tmp_path/"v1.manifest.json").write_text((ROOT/"evals/v1.manifest.json").read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest"):
        runner.load_cases(target)
