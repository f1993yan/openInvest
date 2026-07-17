"""Intraday market monitor entrypoint.

Implementation lives in jobs.market_monitor_* modules; this file keeps the CLI
entrypoint and backwards-compatible imports.
"""
from __future__ import annotations

import requests  # Backwards-compatible monkeypatch target for older tests/tools.

from jobs.market_monitor_common import *  # noqa: F401,F403
from jobs.market_monitor_quotes import *  # noqa: F401,F403
from jobs.market_monitor_notify import *  # noqa: F401,F403
from jobs.market_monitor_entry_exit import *  # noqa: F401,F403
from jobs.market_monitor_guards import *  # noqa: F401,F403
from jobs.market_monitor_alerts import *  # noqa: F401,F403
from jobs.market_monitor_snapshot import *  # noqa: F401,F403
from jobs.market_monitor_runtime import *  # noqa: F401,F403
from jobs.market_monitor_alerts import (  # noqa: F401
    _action_score,
    _has_llm_hold_conflict,
    _likelihood_ratio_score_adjustment,
    _llm_hold_conflict_adjustment,
    _math_review_position_scale,
    _review_conclusion,
    _review_score_adjustment,
    _sell_committee_execution_edge,
    _sell_committee_alert_threshold,
)
from jobs.market_monitor_common import (  # noqa: F401
    _clamp,
    _fmt_price,
    _round_trade_price,
    _safe_num,
)
from jobs.market_monitor_entry_exit import (  # noqa: F401
    _build_initial_position_exit_plan,
    _stock_units,
    _trigger_label,
    _trigger_sides,
    _update_position_exit_plan,
)
from jobs.market_monitor_guards import (  # noqa: F401
    _a_share_limit_up_pct,
    _is_limit_up_buy_blocked,
    _recent_same_direction_committee_trade,
    _result_direction,
)
from jobs.market_monitor_snapshot import (  # noqa: F401
    _first_prefixed_line,
    _llm_operation_review,
    _operation_detail,
)
from jobs.market_monitor_runtime import (  # noqa: F401
    _sync_config_account_fields,
)


if __name__ == "__main__":
    main()
