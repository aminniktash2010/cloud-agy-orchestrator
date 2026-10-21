"""Unit tests: quota, pacing, truncation, spend ledger (E1-S6)."""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from hub.quota import QuotaTracker, SpendLedger, truncate_prompt  # noqa: E402


def test_daily_cap_accounting():
    q = QuotaTracker()
    assert q.check_cap("aistudio", 2) is True
    q.account_attempt("aistudio")
    q.account_attempt("aistudio")
    assert q.check_cap("aistudio", 2) is False
    assert q.usage_today("aistudio") == 2


def test_no_cap_means_unlimited():
    q = QuotaTracker()
    assert q.check_cap("nvidia", None) is True
    for _ in range(50):
        q.account_attempt("nvidia")
    assert q.check_cap("nvidia", None) is True


def test_pacing_wait_math():
    now = [1_000_000.0]
    q = QuotaTracker(now_fn=lambda: now[0])
    q.account_attempt("groq")
    now[0] += 0.5
    assert q.pacing_wait_s("groq", 2.0) == 1.5
    now[0] += 2.0
    assert q.pacing_wait_s("groq", 2.0) == 0.0


def test_truncate_prompt_appends_honesty_note():
    text, truncated = truncate_prompt("x" * 100, 50)
    assert truncated is True
    assert len(text) == 50
    assert "truncated" in text


def test_short_prompt_untouched():
    text, truncated = truncate_prompt("hello", 50)
    assert (text, truncated) == ("hello", False)


def test_spend_ledger_record_and_cap(tmp_path):
    ledger = SpendLedger(path=tmp_path / "spend.json")
    assert ledger.spend_today_usd() == 0.0
    assert ledger.can_spend(0.5, 1.0) is True
    total = ledger.record(0.4)
    assert abs(total - 0.4) < 1e-9
    assert ledger.can_spend(0.7, 1.0) is False
    assert ledger.can_spend(0.5, 1.0) is True
    # ledger file is valid JSON with today's date
    data = json.loads((tmp_path / "spend.json").read_text())
    assert "date" in data and "usd" in data


def test_spend_ledger_rolls_over_daily(tmp_path):
    p = tmp_path / "spend.json"
    p.write_text(json.dumps({"date": "2000-01-01", "usd": 99.0}))
    ledger = SpendLedger(path=p)
    assert ledger.spend_today_usd() == 0.0


def test_estimate_cost_usd():
    cost = SpendLedger.estimate_cost_usd(1_000_000, 500_000, 0.20, 0.50)
    assert abs(cost - 0.45) < 1e-9
