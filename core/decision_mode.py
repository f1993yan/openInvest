"""Decision engine mode helpers.

The production A-share path can run without LLM debate because the executable
decision already comes from deterministic factor, regime, and optimizer logic.
This module keeps the mode vocabulary consistent across server, monitor, and
phone entrypoints.
"""
from __future__ import annotations

import os
from typing import Any


ALGORITHM_ONLY = "algorithm_only"
LLM_COMMITTEE = "llm_committee"


def is_a_share_market(market: Any) -> bool:
    value = str(market or "a").strip().lower()
    return value in {"a", "ashare", "cn", "sh", "sz", "沪深", "a股"}


def normalize_decision_mode(value: Any = "", *, market: Any = "a") -> str:
    """Return ``algorithm_only`` or ``llm_committee``.

    ``INVEST_COMMITTEE_MODE`` is intentionally cost-safe by default: an empty
    value means algorithm-only.  Set it to ``llm`` or ``llm_committee`` to restore
    the old debate path, or ``auto`` to use algorithm-only for A shares and LLM
    debate for other markets.
    """
    raw = str(
        value
        or os.getenv("INVEST_COMMITTEE_MODE")
        or os.getenv("INVEST_DECISION_MODE")
        or ALGORITHM_ONLY
    ).strip().lower()

    if raw in {"auto", "a_share_auto", "ashare_auto"}:
        return ALGORITHM_ONLY if is_a_share_market(market) else LLM_COMMITTEE
    if raw in {
        "llm",
        "llm_committee",
        "committee",
        "debate",
        "committee_debate",
        "full",
    }:
        return LLM_COMMITTEE
    if raw in {
        "",
        "algorithm",
        "algorithm_only",
        "optimizer",
        "optimizer_only",
        "no_llm",
        "off",
        "disabled",
    }:
        return ALGORITHM_ONLY
    return ALGORITHM_ONLY


__all__ = ["ALGORITHM_ONLY", "LLM_COMMITTEE", "is_a_share_market", "normalize_decision_mode"]
