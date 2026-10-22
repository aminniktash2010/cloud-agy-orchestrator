"""Model catalog: normalization, resolution, suggestions (pure logic, no I/O).

Canonical ids: lowercase, dashes, no vendor prefix, dots preserved
(``5.5`` != ``5-5`` — the dash/dot divergence is absorbed by aliases,
never by fuzzy rules). Takes the validated ``ModelCatalogConfig``.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field


def normalize_model_name(text: str) -> str:
    """Deterministic normalization: lowercase, one vendor/ prefix stripped,
    whitespace/underscores -> dashes, repeated dashes collapsed, dots kept."""
    t = (text or "").strip().lower().split("/")[-1]
    t = re.sub(r"[\s_]+", "-", t)
    t = re.sub(r"-{2,}", "-", t)
    return t.strip("-")


@dataclass
class Pin:
    canonical: str
    raw: str
    carriers: dict[str, str]  # tier_id -> provider model id


@dataclass
class PinError:
    kind: str  # "no_match"
    raw: str
    suggestions: list[str] = field(default_factory=list)


class ModelCatalog:
    def __init__(self, cfg) -> None:
        models = getattr(cfg, "models", None) or {}
        self._models = dict(models)
        self._alias_map: dict[str, str] = {}
        for canonical, entry in self._models.items():
            for alias in getattr(entry, "aliases", None) or []:
                na = normalize_model_name(alias)
                if na and na not in self._alias_map:
                    self._alias_map[na] = canonical

    def resolve(self, raw: str) -> tuple[str, object] | None:
        """Returns (canonical_id, carrier_entry) or None. Never guesses."""
        norm = normalize_model_name(raw)
        if not norm:
            return None
        if norm in self._models:
            return norm, self._models[norm]
        canonical = self._alias_map.get(norm)
        if canonical is not None:
            return canonical, self._models[canonical]
        return None

    def suggest(self, raw: str, n: int = 3) -> list[str]:
        """Up to n canonical ids: prefix matches first, then close matches."""
        norm = normalize_model_name(raw)
        candidates: dict[str, str] = {}  # display text -> canonical
        for canonical, entry in self._models.items():
            candidates[canonical] = canonical
            for alias in getattr(entry, "aliases", None) or []:
                candidates.setdefault(alias, canonical)
        out: list[str] = []
        seen: set[str] = set()
        if norm:
            for display in sorted(candidates):
                if normalize_model_name(display).startswith(norm):
                    can = candidates[display]
                    if can not in seen:
                        seen.add(can)
                        out.append(can)
                        if len(out) >= n:
                            return out
        pool = [d for d in candidates if candidates[d] not in seen]
        for display in difflib.get_close_matches(raw, pool, n=n - len(out), cutoff=0.6):
            can = candidates[display]
            if can not in seen:
                seen.add(can)
                out.append(can)
        return out[:n]
