"""On-device AGY CLI quota probe (live-quota Epic C, FR-3.5–FR-3.7, AD-12–AD-14).

Runs INSIDE the Cloud Run container against the same authenticated ``agy``
CLI the adapter uses. Strictly inspection:

* each run first invokes ``agy models`` — the CLI performs its startup quota
  refresh (``doRefreshQuota``, a free summary RPC, not a model call) on launch
  when signed in — then reads the CLI's local quota-summary cache;
* the subprocess argv allowlist is EXACTLY
  ``{agy models, agy --print /usage, agy --print /credits}`` — no ``-p`` /
  ``--prompt`` ever (AC-11: there is a static test asserting this).

No prompt is sent, no model is called, nothing is mutated. The only network
traffic is the CLI's own startup quota refresh, which it performs anyway on
every invocation.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hub.signals import agy_auth_fault

logger = logging.getLogger("model-hub.cli-probe")

# AC-11: the ONLY subprocess invocations the probe may ever make.
PROBE_ARGV_ALLOWLIST: tuple[tuple[str, ...], ...] = (
    ("agy", "models"),
    ("agy", "--print", "/usage"),
    ("agy", "--print", "/credits"),
)

PROBE_TIMEOUT_S = 30

# AD-13: ordered candidate search for the CLI's quota cache; newest mtime wins.
# $AGY_STATE_DIR is honoured first when set; the rest mirror agy-wrapper.sh.
_CACHE_CANDIDATE_DIRS = (
    "$AGY_STATE_DIR",
    "/root/.gemini/antigravity-cli/",
    "/root/.gemini/antigravity/",
    "$HOME/.gemini/antigravity-cli/",
)
_CACHE_FILENAME = "quota_summary.pb"

# probe_available(): agy-wrapper.sh's exact token check order, plus the binary.
_TOKEN_CANDIDATES = (
    "/mnt/cache/antigravity-oauth-token",
    "/root/.gemini/antigravity-cli/antigravity-oauth-token",
)

_NOT_SIGNED_IN_MARKERS = (
    "please sign in",
    "not signed in",
    "sign in to view",
)


@dataclass
class ProbeResult:
    ok: bool
    families: list[dict] = field(default_factory=list)
    # FamilyQuota dicts: {"id","label","models", "remaining_pct" (float|None),
    #                     "resets_at_epoch" (float|None), "note"}
    taxonomy: dict[str, str] = field(default_factory=dict)  # model -> family id
    unmapped: list[dict] = field(default_factory=list)
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    detail: str = ""


# ---------------------------------------------------------------------------
# availability / environment guard (FR-3.7)
# ---------------------------------------------------------------------------
def probe_available(env: Optional[dict] = None) -> bool:
    """True only where the probe can run: an `agy` binary AND an OAuth token.

    Anywhere else (local dev, CI) the probe no-ops and Tier 0 falls back to
    the error-parsing path — tests use recorded fixtures, never a live CLI.
    """
    if shutil.which("agy") is None:
        return False
    env = env if env is not None else os.environ
    if str(env.get("AGY_OAUTH_TOKEN", "")).strip():
        return True
    return any(os.path.isfile(p) for p in _TOKEN_CANDIDATES)


# ---------------------------------------------------------------------------
# subprocess plumbing (single call site — see AC-11 test)
# ---------------------------------------------------------------------------
async def _run_cmd(argv: tuple[str, ...]) -> tuple[int, str]:
    assert argv in PROBE_ARGV_ALLOWLIST, f"probe argv not in allowlist: {argv!r}"
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            list(argv),
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
            cwd=tempfile.gettempdir(),  # quota is account-level; no repo needed
        )
    except subprocess.TimeoutExpired:
        return 124, "probe subprocess timed out"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


# ---------------------------------------------------------------------------
# `agy models` parsing + runtime taxonomy
# ---------------------------------------------------------------------------
def parse_agy_models(text: str) -> list[str]:
    """One model per line; the model name is the first token.

    Defensive: skips progress chatter ("Fetching…") and error lines. The CLI
    has no JSON flag, so this is text parsing against a recorded fixture.
    """
    models: list[str] = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        low = s.lower()
        if low.startswith(("fetching", "error:", "warning:")):
            continue
        token = s.split()[0].strip().strip(",")
        if token and token not in models:
            models.append(token)
    return models


def derive_taxonomy(models: list[str], family_rules: dict) -> dict[str, str]:
    """model name -> family id. Config-order substring match; else "other".

    The probe's runtime model list is authoritative for *which models exist*;
    the config `match` rules assign them to families. More-specific families
    should precede general ones in config (e.g. "argon" before "gemini").
    """
    taxonomy: dict[str, str] = {}
    for m in models:
        ml = m.lower()
        fid = "other"
        for fam_id, rule in (family_rules or {}).items():
            for sub in rule.get("match", []):
                if sub.lower() in ml:
                    fid = fam_id
                    break
            if fid != "other":
                break
        taxonomy[m] = fid
    return taxonomy


# ---------------------------------------------------------------------------
# quota_summary.pb — generic protobuf wire-format walker (AD-12)
# ---------------------------------------------------------------------------
def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result, shift = 0, 0
    while True:
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7
        if shift >= 64:
            raise ValueError("varint overflow")


def _walk_fields(data: bytes):
    """Yield (field_number, wire_type, value) triples.

    value: int for varint (wire 0), bytes for length-delimited (wire 2),
    bytes for 64-bit/32-bit (wires 1/5). Never raises past ValueError/
    IndexError on malformed input — callers treat those as "no buckets".
    """
    pos, end = 0, len(data)
    while pos < end:
        key, pos = _read_varint(data, pos)
        field_no, wire = key >> 3, key & 0x07
        if wire == 0:
            val, pos = _read_varint(data, pos)
        elif wire == 2:
            ln, pos = _read_varint(data, pos)
            val = data[pos:pos + ln]
            pos += ln
        elif wire == 1:
            val, pos = data[pos:pos + 8], pos + 8
        elif wire == 5:
            val, pos = data[pos:pos + 4], pos + 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
        yield field_no, wire, val


def _decode_message(data: bytes) -> Optional[dict]:
    strings: list[str] = []
    varints: dict[int, int] = {}
    subs: list[dict] = []
    try:
        fields = list(_walk_fields(data))
    except (ValueError, IndexError):
        return None
    for _fno, wire, val in fields:
        if wire == 0:
            varints.setdefault(_fno, val)
        elif wire == 2 and isinstance(val, bytes) and val:
            try:
                s = val.decode("utf-8")
            except UnicodeDecodeError:
                s = ""
            if s and "\x00" not in s and all(
                c.isprintable() or c.isspace() for c in s
            ):
                strings.append(s)
            else:
                sub = _decode_message(val)
                if sub and (sub["strings"] or sub["varints"] or sub["subs"]):
                    subs.append(sub)
    return {"strings": strings, "varints": varints, "subs": subs}


def decode_quota_summary(pb: bytes, field_roles: Optional[dict] = None) -> list[dict]:
    """Decode the CLI's quota cache into RawBuckets.

    Returns [{"strings": [...], "varints": {field_no: int}}]. Bucket submessages
    are identified structurally (length-delimited fields containing strings);
    a `response { repeated buckets }` wrapper is descended one level. Unknown
    fields are ignored, never fatal. `field_roles` is NOT needed here — it is
    applied later when mapping buckets to families.
    """
    if not pb:
        return []
    top = _decode_message(pb)
    if top is None:
        return []
    with_strings = [top] if top["strings"] else []
    for sub in top["subs"]:
        if sub["strings"]:
            with_strings.append(sub)
        else:
            # one more level for response-wrapper shapes
            with_strings.extend(s for s in sub["subs"] if s["strings"])
    seen: set[int] = set()
    buckets: list[dict] = []
    for b in with_strings:
        key = id(b)
        if key in seen:
            continue
        seen.add(key)
        buckets.append({"strings": list(b["strings"]), "varints": dict(b["varints"])})
    return buckets


def find_quota_cache() -> Optional[Path]:
    """AD-13: ordered candidate search; newest mtime wins. Logs the pick."""
    best: Optional[Path] = None
    best_mtime = -1.0
    for raw in _CACHE_CANDIDATE_DIRS:
        base = os.path.expandvars(os.path.expanduser(raw))
        if not base or not os.path.isdir(base):
            continue
        try:
            for hit in Path(base).rglob(_CACHE_FILENAME):
                try:
                    mt = hit.stat().st_mtime
                except OSError:
                    continue
                if mt > best_mtime:
                    best, best_mtime = hit, mt
        except OSError:
            continue
    if best is not None:
        logger.info("quota cache picked: %s", best)
    return best


def map_buckets_to_families(
    buckets: list[dict],
    taxonomy: dict[str, str],
    family_rules: dict,
    field_roles: Optional[dict],
) -> tuple[list[dict], list[dict]]:
    """RawBuckets -> (families, unmapped).

    The bucket name (joined string fields) is matched against the family
    `match` substrings. Numeric roles come from config `pb_field_roles`
    (e.g. {"remaining": 1, "limit": 5, "resets_at_epoch": 3}); when
    `field_roles` is None (pending C-S7 verification) every bucket degrades
    honestly: remaining_pct=None, detail-only — numbers are NEVER guessed.
    Multiple buckets per family collapse to the binding (minimum) pct; the
    rest are noted, not dropped.
    """
    per_family: dict[str, list[dict]] = {}
    unmapped: list[dict] = []
    roles = field_roles or {}
    for b in buckets:
        name = " ".join(b["strings"])
        nl = name.lower()
        fid = None
        for fam_id, rule in (family_rules or {}).items():
            if any(str(s).lower() in nl for s in rule.get("match", [])):
                fid = fam_id
                break
        rem = b["varints"].get(roles.get("remaining")) if roles else None
        lim = b["varints"].get(roles.get("limit")) if roles else None
        rst = b["varints"].get(roles.get("resets_at_epoch")) if roles else None
        pct = (100.0 * rem / lim) if (rem is not None and lim) else None
        rec = {
            "name": name,
            "remaining_pct": round(pct, 1) if pct is not None else None,
            "resets_at_epoch": float(rst) if rst else None,
        }
        if fid:
            per_family.setdefault(fid, []).append(rec)
        else:
            unmapped.append(rec)

    families: list[dict] = []
    for fid, rule in (family_rules or {}).items():
        recs = per_family.get(fid, [])
        models = sorted(m for m, f in taxonomy.items() if f == fid)
        if not recs:
            families.append(
                {
                    "id": fid,
                    "label": rule.get("label", fid),
                    "models": models,
                    "remaining_pct": None,
                    "resets_at_epoch": None,
                    "note": "no quota bucket found for this family",
                }
            )
            continue
        binding = min(
            (r for r in recs if r["remaining_pct"] is not None),
            key=lambda r: r["remaining_pct"],
            default=None,
        )
        if binding is None:
            names = "; ".join(r["name"] for r in recs)
            if field_roles is None:
                # C-S7 pending: buckets decoded but numeric roles unverified —
                # honest detail, never guessed numbers.
                note = (
                    "quota cache found; field mapping pending verification"
                    + (f" ({names})" if names else "")
                )
            else:
                note = names or "no usable numeric fields in buckets"
            families.append(
                {
                    "id": fid,
                    "label": rule.get("label", fid),
                    "models": models,
                    "remaining_pct": None,
                    "resets_at_epoch": None,
                    "note": note,
                }
            )
            continue
        others = [r for r in recs if r is not binding]
        note = ""
        if others:
            bits = []
            for r in others:
                pct_s = (
                    f"{r['remaining_pct']:.0f}%"
                    if r["remaining_pct"] is not None
                    else "?"
                )
                bits.append(f"{r['name']}: {pct_s}")
            note = "also: " + "; ".join(bits)
        families.append(
            {
                "id": fid,
                "label": rule.get("label", fid),
                "models": models,
                "remaining_pct": binding["remaining_pct"],
                "resets_at_epoch": binding["resets_at_epoch"],
                "note": note,
            }
        )
    return families, unmapped


# ---------------------------------------------------------------------------
# `/usage` text fallback (FR-3.5c) — best-effort line parser
# ---------------------------------------------------------------------------
def parse_usage_text(text: str, taxonomy: dict[str, str], family_rules: dict) -> list[dict]:
    """Best-effort parse of `agy --print /usage` output into FamilyQuota dicts.

    Looks per text block for: a family name (match substrings), a percent
    ("23%"), and a reset phrase ("refresh in 5 days, 6 hours" / "resets in …").
    Anything unparseable yields remaining_pct=None — honest, never guessed.
    """
    import re

    families: dict[str, dict] = {}
    blocks = re.split(r"\n\s*\n", text or "")
    for block in blocks:
        bl = block.lower()
        fid = None
        for fam_id, rule in (family_rules or {}).items():
            if any(str(s).lower() in bl for s in rule.get("match", [])):
                fid = fam_id
                break
        if fid is None:
            continue
        pct_m = re.search(r"(\d+(?:\.\d+)?)\s*%", block)
        pct = float(pct_m.group(1)) if pct_m else None
        rst_m = re.search(
            r"(?:refresh(?:es)?|reset(?:s)?)\s+in\s+([^\n.]+)", block, re.IGNORECASE
        )
        reset_raw = rst_m.group(1).strip() if rst_m else None
        rec = families.get(fid)
        if rec is None:
            rec = {
                "id": fid,
                "label": (family_rules.get(fid) or {}).get("label", fid),
                "models": sorted(m for m, f in taxonomy.items() if f == fid),
                "remaining_pct": pct,
                "resets_at_epoch": None,
                "note": f"parsed from /usage text{f'; resets {reset_raw}' if reset_raw else ''}",
            }
            families[fid] = rec
        elif pct is not None and (
            rec["remaining_pct"] is None or pct < rec["remaining_pct"]
        ):
            rec["remaining_pct"] = pct  # binding (minimum) wins
    # every configured family gets a row, even with no data
    out = []
    for fid, rule in (family_rules or {}).items():
        if fid in families:
            out.append(families[fid])
        else:
            out.append(
                {
                    "id": fid,
                    "label": rule.get("label", fid),
                    "models": sorted(m for m, f in taxonomy.items() if f == fid),
                    "remaining_pct": None,
                    "resets_at_epoch": None,
                    "note": "not reported by /usage",
                }
            )
    return out


# ---------------------------------------------------------------------------
# the probe run
# ---------------------------------------------------------------------------
async def run_probe(agy_cfg: dict, env: Optional[dict] = None) -> Optional[ProbeResult]:
    """One probe cycle. Returns None when the probe cannot run in this
    environment (FR-3.7); otherwise a ProbeResult with ok True/False."""
    now = datetime.now(timezone.utc)
    if not probe_available(env):
        return None
    family_rules = (agy_cfg or {}).get("families", {})

    # 1. `agy models` — also triggers the CLI's startup quota refresh.
    rc, out = await _run_cmd(("agy", "models"))
    low = out.lower()
    if rc != 0 or agy_auth_fault(out) or any(m in low for m in _NOT_SIGNED_IN_MARKERS):
        return ProbeResult(
            ok=False, at=now, detail="agy CLI not authenticated (sign-in required)"
        )
    models = parse_agy_models(out)
    if not models:
        return ProbeResult(
            ok=False, at=now, detail="could not parse `agy models` output"
        )
    taxonomy = derive_taxonomy(models, family_rules)

    # 2. quota_summary.pb cache (AD-12/AD-13).
    cache = find_quota_cache()
    if cache is not None:
        try:
            raw = cache.read_bytes()
        except OSError as e:
            logger.warning("quota cache unreadable (%s): %s", cache, e)
            raw = b""
        buckets = decode_quota_summary(raw, (agy_cfg or {}).get("pb_field_roles"))
        if buckets:
            families, unmapped = map_buckets_to_families(
                buckets, taxonomy, family_rules, (agy_cfg or {}).get("pb_field_roles")
            )
            return ProbeResult(
                ok=True,
                families=families,
                taxonomy=taxonomy,
                unmapped=unmapped,
                at=now,
                detail=f"quota cache {cache.name}",
            )
        logger.warning("quota cache present but undecodable: %s", cache)

    # 3. `/usage` text fallback (FR-3.5c).
    rc2, out2 = await _run_cmd(("agy", "--print", "/usage"))
    if rc2 == 0 and out2.strip():
        families = parse_usage_text(out2, taxonomy, family_rules)
        return ProbeResult(
            ok=True,
            families=families,
            taxonomy=taxonomy,
            unmapped=[],
            at=now,
            detail="/usage text fallback",
        )
    return ProbeResult(
        ok=False,
        at=now,
        detail="no quota cache and /usage fallback failed",
    )
