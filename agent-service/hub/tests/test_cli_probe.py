"""Unit tests for hub/cli_probe.py (live-quota Epic C).

FIXTURE HONESTY (C-S7): no real Cloud Run capture was obtainable in this
environment — the deployed CLI's interactive TUI could not be driven to a
models list, and quota_summary.pb is unreadable without an authenticated
session. Fixtures below are SYNTHETIC, shaped by the CLI's documented
behavior; the parser is defensive by design and degrades honestly when the
real bytes differ. The `field_roles`-dependent assertions are marked and must
be re-verified against the first real capture.
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from hub import cli_probe  # noqa: E402
from hub.cli_probe import (  # noqa: E402
    PROBE_ARGV_ALLOWLIST,
    decode_quota_summary,
    derive_taxonomy,
    find_quota_cache,
    map_buckets_to_families,
    parse_agy_models,
    parse_usage_text,
    probe_available,
)

FAMILY_RULES = {
    "argon": {"label": "Gemini Argon-class", "match": ["argon"]},
    "gemini": {"label": "Gemini Models", "match": ["gemini"]},
    "claude_gpt": {"label": "Claude and GPT models", "match": ["claude", "gpt"]},
}

# Synthetic `agy models` fixture (see module docstring).
AGY_MODELS_FIXTURE = {
    "output": (
        "Fetching available models...\n"
        "gemini-3.8-flash-high\n"
        "gemini-3.8-flash-medium\n"
        "gemini-3.8-flash-low\n"
        "claude-sonnet-4.6\n"
        "gpt-oss-120b\n"
        "gemini-4-argon\n"
    ),
    "expected_taxonomy": {
        "gemini-3.8-flash-high": "gemini",
        "gemini-3.8-flash-medium": "gemini",
        "gemini-3.8-flash-low": "gemini",
        "claude-sonnet-4.6": "claude_gpt",
        "gpt-oss-120b": "claude_gpt",
        "gemini-4-argon": "argon",
    },
}

# Synthetic `/usage` text fixture (see module docstring).
USAGE_TEXT_FIXTURE = """\
Gemini Models
Weekly Limit Remaining 23% — refreshes in 5 days, 6 hours.
Five Hour Limit Remaining 100%.

Claude and GPT models
Weekly Limit Remaining 0% — refreshes in 5 days, 12 hours.

Gemini 4 Argon
Weekly Limit Remaining 0% — refreshes in 5 days, 23 hours.
"""


# ---------------------------------------------------------------------------
# protobuf wire-format helpers (test-only encoders for hand-crafted fixtures)
# ---------------------------------------------------------------------------
def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _field(num: int, wire: int, payload: bytes) -> bytes:
    return _varint((num << 3) | wire) + payload


def _str(num: int, s: str) -> bytes:
    data = s.encode()
    return _field(num, 2, _varint(len(data)) + data)


def _vn(num: int, v: int) -> bytes:
    return _field(num, 0, _varint(v))


def _msg(num: int, inner: bytes) -> bytes:
    return _field(num, 2, _varint(len(inner)) + inner)


def _bucket(name: str, remaining: int, limit: int, resets_epoch: int) -> bytes:
    # field roles under test: remaining=2, limit=5, resets_at_epoch=3
    inner = _str(1, name) + _vn(2, remaining) + _vn(3, resets_epoch) + _vn(5, limit)
    return _msg(1, inner)


# ---------------------------------------------------------------------------
def test_parse_agy_models_from_fixture():
    models = parse_agy_models(AGY_MODELS_FIXTURE["output"])
    # the mapping comes from the fixture, not from hardcoded lists in the test
    assert models == list(AGY_MODELS_FIXTURE["expected_taxonomy"].keys())


def test_derive_taxonomy_from_fixture():
    models = parse_agy_models(AGY_MODELS_FIXTURE["output"])
    assert derive_taxonomy(models, FAMILY_RULES) == AGY_MODELS_FIXTURE["expected_taxonomy"]


def test_derive_taxonomy_unknown_model_goes_other():
    assert derive_taxonomy(["some-future-model-9"], FAMILY_RULES) == {
        "some-future-model-9": "other"
    }


def test_decode_quota_summary_wire_format():
    pb = (
        _bucket("gemini-weekly", 23, 100, 1790000000)
        + _bucket("claude-weekly", 0, 100, 1790003600)
    )
    buckets = decode_quota_summary(pb, None)
    assert len(buckets) == 2
    by_name = {b["strings"][0]: b for b in buckets}
    assert by_name["gemini-weekly"]["varints"] == {2: 23, 3: 1790000000, 5: 100}
    assert by_name["claude-weekly"]["varints"][2] == 0


def test_decode_quota_summary_malformed_never_raises():
    assert decode_quota_summary(b"\xff\xff\xff\xff\xff", None) == []
    assert decode_quota_summary(b"", None) == []
    assert decode_quota_summary(b"\x08", None) == []  # truncated varint


def test_decode_quota_summary_descends_into_wrapper():
    inner = _bucket("gemini-weekly", 23, 100, 1790000000)
    wrapped = _msg(7, inner)  # response { ... } wrapper, unknown field number
    buckets = decode_quota_summary(wrapped, None)
    assert len(buckets) == 1
    assert buckets[0]["strings"] == ["gemini-weekly"]


def test_map_buckets_with_verified_field_roles():
    """Field roles verified against the hand-crafted fixture (C-S7 stand-in)."""
    pb = _bucket("gemini-weekly", 23, 100, 1790000000) + _bucket(
        "mystery-bucket", 50, 100, 1790000000
    )
    buckets = decode_quota_summary(pb, None)
    roles = {"remaining": 2, "limit": 5, "resets_at_epoch": 3}
    families, unmapped = map_buckets_to_families(
        buckets,
        {"gemini-3.8-flash-high": "gemini"},
        FAMILY_RULES,
        roles,
    )
    by_id = {f["id"]: f for f in families}
    assert by_id["gemini"]["remaining_pct"] == 23.0
    assert by_id["gemini"]["resets_at_epoch"] == 1790000000.0
    assert by_id["gemini"]["models"] == ["gemini-3.8-flash-high"]
    assert [u["name"] for u in unmapped] == ["mystery-bucket"]
    assert unmapped[0]["remaining_pct"] == 50.0


def test_map_buckets_without_field_roles_degrades_honestly():
    """C-S7 pending: no verified roles -> remaining_pct None, honest note."""
    pb = _bucket("gemini-weekly", 23, 100, 1790000000)
    buckets = decode_quota_summary(pb, None)
    families, _unmapped = map_buckets_to_families(
        buckets, {}, FAMILY_RULES, None  # field_roles None = unverified
    )
    by_id = {f["id"]: f for f in families}
    assert by_id["gemini"]["remaining_pct"] is None
    assert "field mapping pending verification" in by_id["gemini"]["note"]


def test_map_buckets_binding_is_minimum():
    pb = _bucket("gemini-weekly", 23, 100, 1790000000) + _bucket(
        "gemini-5hour", 100, 100, 1790007200
    )
    buckets = decode_quota_summary(pb, None)
    roles = {"remaining": 2, "limit": 5, "resets_at_epoch": 3}
    families, _u = map_buckets_to_families(buckets, {}, FAMILY_RULES, roles)
    gem = {f["id"]: f for f in families}["gemini"]
    assert gem["remaining_pct"] == 23.0  # binding (minimum) wins
    assert "gemini-5hour" in gem["note"]  # the other bucket is noted, not dropped


def test_parse_usage_text_fallback():
    fams = parse_usage_text(
        USAGE_TEXT_FIXTURE, AGY_MODELS_FIXTURE["expected_taxonomy"], FAMILY_RULES
    )
    by_id = {f["id"]: f for f in fams}
    assert by_id["gemini"]["remaining_pct"] == 23.0
    assert by_id["claude_gpt"]["remaining_pct"] == 0.0
    assert by_id["argon"]["remaining_pct"] == 0.0
    assert "resets" in by_id["gemini"]["note"]


def test_parse_usage_text_unparseable_is_honest():
    fams = parse_usage_text(
        "some unrelated banner text\nno percentages here\n",
        {}, FAMILY_RULES,
    )
    assert all(f["remaining_pct"] is None for f in fams)
    assert {f["id"] for f in fams} == set(FAMILY_RULES)


def test_probe_available_no_binary(monkeypatch):
    monkeypatch.setattr("hub.cli_probe.shutil.which", lambda _n: None)
    assert probe_available({}) is False


def test_probe_available_needs_token(monkeypatch, tmp_path):
    monkeypatch.setattr("hub.cli_probe.shutil.which", lambda _n: "/usr/local/bin/agy")
    # no token anywhere -> False
    monkeypatch.setattr(
        "hub.cli_probe._TOKEN_CANDIDATES", (str(tmp_path / "nope"),)
    )
    assert probe_available({}) is False
    # AGY_OAUTH_TOKEN env counts
    assert probe_available({"AGY_OAUTH_TOKEN": "ya29.x"}) is True
    # token file counts
    tok = tmp_path / "tok"
    tok.write_text("ya29.x")
    monkeypatch.setattr("hub.cli_probe._TOKEN_CANDIDATES", (str(tok),))
    assert probe_available({}) is True


def test_find_quota_cache_newest_mtime_wins(tmp_path, monkeypatch):
    d1, d2 = tmp_path / "a", tmp_path / "b"
    (d1 / "sub").mkdir(parents=True)
    (d2).mkdir(parents=True)
    old = d1 / "sub" / "quota_summary.pb"
    new = d2 / "quota_summary.pb"
    old.write_bytes(b"old")
    new.write_bytes(b"new")
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    monkeypatch.setattr(
        "hub.cli_probe._CACHE_CANDIDATE_DIRS", (str(d1), str(d2))
    )
    assert find_quota_cache() == new


def test_find_quota_cache_missing_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "hub.cli_probe._CACHE_CANDIDATE_DIRS", (str(tmp_path / "empty"),)
    )
    assert find_quota_cache() is None
