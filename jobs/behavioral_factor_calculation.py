"""Production A-share behavioral factor calculation task.

The full cross-section remains available internally for zero-weight targets;
the public weekly selection cache lists only the selected top four.
"""
from __future__ import annotations

import logging
import json
from pathlib import Path
from typing import Dict, Any
from datetime import datetime

from jobs.market_monitor_runtime import load_config
from db.account_ledger import AccountLedger, REAL_ACCOUNT
from jobs.market_monitor_quotes import build_behavioral_factor_context

log = logging.getLogger("jobs.behavioral_factor_calculation")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ASSESSMENTS_FILE = PROJECT_ROOT / "data" / "behavioral_factor_assessments.json"


def run() -> str:
    """Sunday task to calculate and cache A-share behavioral factors for all targets."""
    log.info("Starting Sunday A-share behavioral factor calculation task...")

    # 1. Load config and targets
    try:
        config = load_config()
        ledger = AccountLedger()
        ledger.ensure_initialized(config)
        holdings = config.get("holdings", [])
        watchlist = config.get("watchlist", [])
        all_stocks = holdings + watchlist
    except Exception as e:
        log.error(f"Failed to load stocks for calculation: {e}")
        return f"Error: {e}"

    if not all_stocks:
        log.info("No stocks configured in holdings or watchlist.")
        return "No stocks configured."

    # 2. Extract A-share symbols
    symbols = sorted({
        str(stock.get("symbol") or "").strip().upper()
        for stock in all_stocks
        if str(stock.get("market") or "a").lower() == "a"
        and len(str(stock.get("symbol") or "").strip()) == 6
    })

    if not symbols:
        log.info("No A-share symbols found in targets.")
        return "No A-share symbols found."

    log.info(f"Calculating factors for {len(symbols)} A-shares: {symbols}")

    # 3. Pull 2y histories and assess
    from utils.market_data_provider import get_history_data
    from core.ashare_behavioral_factor import assess_behavioral_universe
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from utils.safe_persistence import atomic_write_json

    histories: Dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(symbols))) as pool:
        futures = {pool.submit(get_history_data, symbol, "2y"): symbol for symbol in symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                frame = future.result()
                if frame is not None and not frame.empty:
                    histories[symbol] = frame
            except Exception as exc:
                log.warning(f"A股行为因子历史数据缺失 {symbol}: {exc}")

    if not histories:
        log.warning("No historical data fetched successfully.")
        return "No historical data fetched."

    assessments = assess_behavioral_universe(histories)
    if not assessments:
        log.warning("Assessments failed to generate.")
        return "Assessments failed."

    # The committee still receives the full cross-section through the state
    # update below.  This user-facing weekly selection cache contains only the
    # model target members so non-selected candidates are never presented as
    # weekly picks.
    assessments_file = ASSESSMENTS_FILE
    assessments_file.parent.mkdir(parents=True, exist_ok=True)

    serializable = {
        symbol: assessment.as_dict()
        for symbol, assessment in assessments.items()
        if assessment.selected
    }

    try:
        atomic_write_json(assessments_file, {
            "date": datetime.now().isoformat(),
            "selection_scope": "factor_model_target_portfolio",
            "candidate_count": len(assessments),
            "selected_count": len(serializable),
            "assessments": serializable
        })
        log.info(
            "Saved %s selected assessments from %s candidates to %s",
            len(serializable),
            len(assessments),
            assessments_file,
        )
    except Exception as exc:
        log.error(f"Failed to save assessments file: {exc}")
        return f"Failed to save output: {exc}"

    # Also update the rebalance state data/behavioral_factor_state.json using the quotes helper
    try:
        build_behavioral_factor_context(all_stocks)
        log.info("Successfully updated behavioral factor state (rebalance anchors).")
    except Exception as e:
        log.warning(f"Failed to update behavioral factor state: {e}")

    return f"Selected {len(serializable)} of {len(assessments)} symbols."
