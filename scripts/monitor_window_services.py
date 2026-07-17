"""Data loading, trading, and committee service helpers for the desktop monitor window."""
from __future__ import annotations

import json
import logging
import math
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from scripts.monitor_window_constants import (
    BACKGROUND_PROCESS_PATTERNS,
    DAILY_SELECTION_LATEST,
    ROOT,
    STATE_PRIORITY,
    VERDICT_PRIORITY,
    WEEKEND_NEWS_DIR,
)
from jobs.trading_mode import DEFAULT_TRADING_MODE, normalize_trading_mode, trading_mode_label, trading_mode_payload

SECTOR_CACHE_PATH = ROOT / "data" / "sector_cache.json"
MARKET_DB_PATH = ROOT / "db" / "market_data.db"
log = logging.getLogger(__name__)


def _remote_api_headers() -> Dict[str, str]:
    """Return optional Bearer auth without changing unauthenticated installs."""
    token = str(os.getenv("INVEST_REMOTE_API_TOKEN") or os.getenv("INVEST_API_TOKEN") or "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}


def _safe_num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _clean_sector(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in {"", "unknown", "none", "null", "-"} or text in {"未分组", "全局"} else text


def _symbol_variants(symbol: Any) -> List[str]:
    text = str(symbol or "").strip().upper()
    if not text:
        return []
    variants = [text]
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) == 6:
        variants.extend([digits, f"SH{digits}", f"SZ{digits}", f"{digits}.SH", f"{digits}.SZ", f"{digits}.SS"])
    out: List[str] = []
    for item in variants:
        if item and item not in out:
            out.append(item)
    return out


def _load_sector_cache(path: Path = SECTOR_CACHE_PATH) -> Dict[str, str]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    mapping = data.get("mapping") if isinstance(data, dict) else {}
    if not isinstance(mapping, dict):
        return {}
    return {
        str(symbol).strip().upper(): _clean_sector(sector)
        for symbol, sector in mapping.items()
        if str(symbol).strip() and _clean_sector(sector)
    }


def _sector_from_cache(symbol: Any, cache: Dict[str, str]) -> str:
    for key in _symbol_variants(symbol):
        sector = _clean_sector(cache.get(key))
        if sector:
            return sector
    return ""


def _fill_missing_row_sectors(rows: List[Dict[str, Any]], cache: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    cache = cache if cache is not None else _load_sector_cache()
    if not cache:
        return rows
    out: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        sector = _clean_sector(item.get("sector")) or _clean_sector(item.get("industry"))
        if not sector:
            sector = _sector_from_cache(item.get("symbol"), cache)
            if sector:
                item["sector"] = sector
                item["industry"] = _clean_sector(item.get("industry")) or sector
                item.setdefault("sector_source", "eastmoney_sector_cache")
        out.append(item)
    return out


def _stop_background_services() -> None:
    """Stop OpenInvest background workers started for the desktop window."""
    if os.name != "nt":
        return
    root = str(ROOT).replace("'", "''").lower()
    patterns = ",".join(f"'{pattern}'" for pattern in BACKGROUND_PROCESS_PATTERNS)
    script = f"""
$root = '{root}'
$patterns = @({patterns})
$all = @(Get-CimInstance Win32_Process)
$targets = @($all | Where-Object {{
  $cmd = ($_.CommandLine + '').ToLowerInvariant()
  $exe = ($_.ExecutablePath + '').ToLowerInvariant()
  $name = ($_.Name + '').ToLowerInvariant()
  ($name -match '^(cmd|uv|uvicorn|python|pythonw)\\.exe$') -and
  ($cmd.Contains($root) -or $exe.Contains($root)) -and
  ($patterns | Where-Object {{ $cmd.Contains($_) }})
}})
foreach ($target in $targets) {{
  Stop-Process -Id $target.ProcessId -Force -ErrorAction SilentlyContinue
}}
foreach ($target in $targets) {{
  Wait-Process -Id $target.ProcessId -Timeout 5 -ErrorAction SilentlyContinue
}}
"""
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


def _fmt_price(value: Any) -> str:
    value = _safe_num(value)
    return f"{value:.2f}" if value > 0 else "-"


def _fmt_money(value: Any) -> str:
    value = _safe_num(value)
    if value == 0:
        return "0"
    return f"{value:,.0f}" if abs(value) >= 1 else "-"


def _fmt_cash_line(cash: Any, total_assets: Any, pending_cash: Any = 0) -> str:
    cash_value = _safe_num(cash)
    pending_value = _safe_num(pending_cash)
    total_value = _safe_num(total_assets)
    pct = cash_value / total_value * 100.0 if total_value > 0 else 0.0
    if pending_value > 0:
        return f"可用 {_fmt_money(cash_value)} / {pct:.1f}% · T+2 {_fmt_money(pending_value)}"
    return f"现金 {_fmt_money(cash_value)} / {pct:.1f}%"


def _correct_real_cash(cash: float, t2_pending: float) -> Dict[str, Any]:
    if cash < 0 or t2_pending < 0:
        raise ValueError("金额不能为负数")
    from jobs.market_monitor import load_config
    from db.account_ledger import AccountLedger, REAL_ACCOUNT

    ledger = AccountLedger()
    ledger.ensure_initialized(load_config())
    ledger.correct_cash(REAL_ACCOUNT, cash, t2_pending)
    return ledger.account_summary(REAL_ACCOUNT)


def _current_trading_mode() -> Dict[str, str]:
    try:
        from jobs.market_monitor import load_config

        config = load_config()
        return trading_mode_payload(config.get("trading_mode", DEFAULT_TRADING_MODE))
    except Exception:
        return trading_mode_payload(DEFAULT_TRADING_MODE)


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "enable", "enabled", "开", "开启"}:
        return True
    if text in {"0", "false", "no", "n", "off", "disable", "disabled", "关", "关闭"}:
        return False
    return default


def _current_action_email_enabled() -> bool:
    try:
        from jobs.market_monitor import load_config

        config = load_config()
        return _coerce_bool(config.get("monitor_action_email_enabled"), True)
    except Exception:
        return True


def _set_action_email_enabled(enabled: bool) -> bool:
    value = bool(enabled)
    from jobs.market_monitor import CONFIG_PATH, load_config

    config = load_config()
    config["monitor_action_email_enabled"] = value
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    snapshot_path = ROOT / "data" / "market_monitor" / "latest_window.json"
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8")) if snapshot_path.exists() else {}
    except Exception:
        payload = {}
    if isinstance(payload, dict) and payload:
        payload["monitor_action_email_enabled"] = value
        payload["message"] = f"执行邮件已{'开启' if value else '关闭'}"
        _write_snapshot(snapshot_path, payload)
    return value


def _set_trading_mode(mode: str) -> Dict[str, str]:
    normalized = normalize_trading_mode(mode)
    from jobs.market_monitor import CONFIG_PATH, load_config

    config = load_config()
    config["trading_mode"] = normalized
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    snapshot_path = ROOT / "data" / "market_monitor" / "latest_window.json"
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8")) if snapshot_path.exists() else {}
    except Exception:
        payload = {}
    if isinstance(payload, dict) and payload:
        payload["trading_mode"] = trading_mode_payload(normalized)
        payload["message"] = f"交易模式已切换为{trading_mode_label(normalized)}"
        _write_snapshot(snapshot_path, payload)
    return trading_mode_payload(normalized)


def _cash_ratio(cash: Any, total_assets: Any) -> float:
    cash_value = max(_safe_num(cash), 0.0)
    total_value = max(_safe_num(total_assets), 0.0)
    if total_value <= 0:
        return 0.0
    return max(0.0, min(cash_value / total_value, 1.0))


def _fmt_update_time(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "更新 -"
    try:
        dt = datetime.fromisoformat(text)
        return f"更新 {dt.strftime('%H:%M:%S')}"
    except Exception:
        return f"更新 {text}"


def _fmt_lots(value: float) -> str:
    if abs(value) >= 1:
        return f"{math.floor(abs(value))}手"
    return "-"


def _fmt_holding_lots(row: Dict[str, Any]) -> str:
    units = _safe_num(row.get("units"))
    lot_size = _lot_size(row)
    if units <= 0 or lot_size <= 0:
        return "0手"
    lots = units / lot_size
    if abs(lots - round(lots)) < 1e-6:
        return f"{int(round(lots))}手"
    return f"{lots:.1f}手"


def _max_executable_lots(row: Dict[str, Any]) -> int:
    return max(0, math.floor(abs(_suggested_lots(row))))


def _suggested_lots(row: Dict[str, Any]) -> float:
    op = row.get("operation") or {}
    alloc = _safe_num(op.get("suggested_alloc_cny"))
    price = _safe_num((row.get("price") or {}).get("current"))
    lot_size = _safe_num(row.get("min_lot_size"), 100)
    if alloc == 0 or price <= 0 or lot_size <= 0:
        return 0.0
    return alloc / (price * lot_size)


def _lot_size(row: Dict[str, Any]) -> int:
    return int(_safe_num(row.get("min_lot_size"), 100) or 100)


def _operation_direction(row: Dict[str, Any]) -> str:
    op = row.get("operation") or {}
    verdict = str(op.get("verdict") or "").upper()
    alloc = _safe_num(op.get("suggested_alloc_cny"))
    lots = _suggested_lots(row)
    if verdict in {"SELL", "TRIM"} or alloc < 0 or lots < 0:
        return "SELL"
    return "BUY"


def _execute_user_trade_from_row(row: Dict[str, Any], lots: float) -> Dict[str, Any]:
    symbol = str(row.get("symbol") or "").strip()
    if not symbol:
        raise ValueError("缺少标的代码")
    if lots <= 0 or int(lots) != lots:
        raise ValueError("手数必须是正整数")
    max_lots = _max_executable_lots(row)
    if max_lots <= 0:
        raise ValueError("推荐手数不足 1 手，已取消记账")
    if lots > max_lots:
        raise ValueError(f"手数不能超过推荐上限 {max_lots} 手")
    from jobs.market_monitor import fetch_sina_prices, load_config

    price_info = fetch_sina_prices([symbol]).get(symbol) or {}
    price = _safe_num(price_info.get("price"))
    if price <= 0:
        raise ValueError("无法获取最新现价，已取消记账")
    units = int(lots) * _lot_size(row)
    direction = _operation_direction(row)
    from db.account_ledger import AccountLedger

    ledger = AccountLedger()
    ledger.ensure_initialized(load_config())
    operation = row.get("operation") or {}
    decision_id = str(row.get("decision_id") or operation.get("decision_id") or "").strip() or None
    idempotency_key = (
        f"user:{decision_id}:{direction}:{units}"
        if decision_id
        else f"user-manual:{symbol}:{direction}:{units}:{datetime.now().isoformat(timespec='microseconds')}"
    )
    trade = ledger.apply_user_trade(
        symbol=symbol,
        direction=direction,
        units=units,
        price=price,
        note="monitor_window_user_confirmed",
        decision_id=decision_id,
        idempotency_key=idempotency_key,
    )
    name = price_info.get("name") or row.get("name") or symbol
    return {
        "message": f"已按最新价记账: {name} {trade.direction} {trade.units:.0f}股 @ {trade.price:.2f}",
        "trade": trade.__dict__,
    }


def _execute_user_trade_manual(symbol: str, direction: str, lots: int, lot_size: int = 100) -> str:
    symbol = str(symbol or "").strip()
    direction = str(direction or "").upper()
    if not symbol:
        raise ValueError("缺少标的代码")
    if direction not in {"BUY", "SELL"}:
        raise ValueError("方向必须是 BUY 或 SELL")
    if lots <= 0:
        raise ValueError("手数必须大于 0")
    lot_size = max(1, int(_safe_num(lot_size, 100) or 100))
    from jobs.market_monitor import fetch_sina_prices, load_config

    price_info = fetch_sina_prices([symbol]).get(symbol) or {}
    price = _safe_num(price_info.get("price"))
    if price <= 0:
        raise ValueError("无法获取最新现价，已取消记账")
    from db.account_ledger import AccountLedger

    ledger = AccountLedger()
    ledger.ensure_initialized(load_config())
    trade = ledger.apply_user_trade(
        symbol=symbol,
        direction=direction,
        units=int(lots) * lot_size,
        price=price,
        note="monitor_window_manual_trade",
        idempotency_key=(
            f"user-manual:{symbol}:{direction}:{int(lots) * lot_size}:"
            f"{datetime.now().isoformat(timespec='microseconds')}"
        ),
    )
    name = price_info.get("name") or symbol
    return f"已按最新价记账: {name} {trade.direction} {trade.units:.0f}股 @ {trade.price:.2f}"


def _is_config_watch_only(row: Dict[str, Any]) -> bool:
    symbol = str(row.get("symbol") or "").strip()
    if not symbol or row.get("_demo") or row.get("is_holding") or _safe_num(row.get("units")) > 0:
        return False
    try:
        from jobs.market_monitor import load_config

        config = load_config()
        holding_symbols = {
            str(item.get("symbol") or "").strip()
            for item in config.get("holdings") or []
            if item.get("symbol")
        }
        watch_symbols = {
            str(item.get("symbol") or "").strip()
            for item in config.get("watchlist") or []
            if item.get("symbol")
        }
        try:
            from db.account_ledger import AccountLedger, REAL_ACCOUNT

            ledger = AccountLedger()
            ledger.ensure_initialized(config)
            ledger_units = max(
                (
                    _safe_num(item.get("units"))
                    for item in ledger.list_holdings(REAL_ACCOUNT)
                    if str(item.get("symbol") or "").strip() == symbol
                ),
                default=0.0,
            )
            if ledger_units > 0:
                return False
        except Exception:
            pass
        return symbol in watch_symbols and symbol not in holding_symbols
    except Exception:
        return False


def _remove_from_watchlist(symbol: str) -> str:
    symbol = str(symbol or "").strip()
    if not symbol:
        raise ValueError("缺少标的代码")
    from jobs.market_monitor import load_config
    from db.account_ledger import AccountLedger, REAL_ACCOUNT

    config = load_config()
    ledger = AccountLedger()
    ledger.ensure_initialized(config)
    target = next(
        (
            item
            for item in ledger.list_holdings(REAL_ACCOUNT)
            if str(item.get("symbol") or "").strip() == symbol
        ),
        None,
    )
    if not target:
        return "该标的不在关注列表"
    if _safe_num(target.get("units")) > 0:
        raise ValueError("当前标的是持仓，不能从主窗口取消关注")
    if not ledger.delete_holding(REAL_ACCOUNT, symbol):
        raise ValueError("取消关注失败")
    return f"已取消关注: {symbol}"


def _filter_rows_by_current_config(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    try:
        from jobs.market_monitor import load_config

        config = load_config()
        allowed_symbols = {
            str(item.get("symbol") or "").strip()
            for bucket in (config.get("holdings") or [], config.get("watchlist") or [])
            for item in bucket
            if item.get("symbol")
        }
        if not allowed_symbols:
            return rows
        return [
            row
            for row in rows
            if str(row.get("symbol") or "").strip() in allowed_symbols
        ]
    except Exception:
        return rows


def _fmt_pct(value: Any) -> str:
    return f"{_safe_num(value):+.2f}%"


def _short(text: Any, limit: int = 100) -> str:
    value = str(text or "").strip().replace("\n", " ")
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "..."


def _load_snapshot(path: Path) -> Dict[str, Any]:
    fallback = _load_config_snapshot(f"暂无监控快照: {path}", source_path=path)
    if not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(fallback, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        return fallback
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if _looks_like_sample_snapshot(payload):
            clean_snap = _load_config_snapshot("忽略旧示例快照，已改用本地持仓配置", source_path=path)
            try:
                path.write_text(json.dumps(clean_snap, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
            return clean_snap
        return payload
    except Exception as exc:  # noqa: BLE001
        return _load_config_snapshot(f"读取监控快照失败: {type(exc).__name__}: {exc}", source_path=path)


def _write_snapshot(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _recount_snapshot_rows(payload: Dict[str, Any]) -> Dict[str, Any]:
    rows = list(payload.get("rows") or [])
    counts = dict(payload.get("counts") or {})
    counts.update(
        {
            "symbols": len(rows),
            "action_required": sum(1 for row in rows if row.get("state") == "action_required"),
            "errors": sum(1 for row in rows if not row.get("success", True)),
        }
    )
    payload["counts"] = counts
    return payload


def _merge_snapshot_row(payload: Dict[str, Any], row: Dict[str, Any]) -> Dict[str, Any]:
    symbol = str(row.get("symbol") or "").upper()
    if not symbol:
        return payload
    rows = list(payload.get("rows") or [])
    replaced = False
    merged_rows: List[Dict[str, Any]] = []
    for item in rows:
        if str(item.get("symbol") or "").upper() == symbol:
            merged_rows.append(row)
            replaced = True
        else:
            merged_rows.append(item)
    if not replaced:
        merged_rows.append(row)
    payload["rows"] = _sort_stock_rows(merged_rows)
    now = datetime.now().isoformat(timespec="seconds")
    payload["generated_at"] = now
    payload["round_time"] = "manual_committee"
    return _recount_snapshot_rows(payload)


def _update_snapshot_row(path: Path, row: Dict[str, Any]) -> Dict[str, Any]:
    payload = _load_snapshot(path)
    payload = _merge_snapshot_row(payload, row)
    _write_snapshot(path, payload)
    return payload



def _looks_like_sample_snapshot(payload: Dict[str, Any]) -> bool:
    rows = payload.get("rows") or []
    return (
        len(rows) == 1
        and str(rows[0].get("symbol") or "") == "600900"
        and "示例" in str(rows[0].get("name") or "")
    )


def _load_config_snapshot(message: str = "", *, source_path: Optional[Path] = None) -> Dict[str, Any]:
    try:
        from jobs.market_monitor import fetch_sina_prices, load_config
        from jobs.market_monitor import CONFIG_PATH

        config = load_config()
        timestamp_path = source_path if source_path and source_path.exists() else CONFIG_PATH
        stocks = list(config.get("holdings") or []) + list(config.get("watchlist") or [])
        symbols = [str(stock.get("symbol") or "").strip() for stock in stocks if stock.get("symbol")]
        prices = fetch_sina_prices(symbols) if symbols else {}
    except SystemExit:
        config = {}
        stocks = []
        prices = {}
        timestamp_path = source_path
    except Exception:
        config = {}
        stocks = []
        prices = {}
        timestamp_path = source_path

    total_assets = _safe_num(config.get("total_assets"))
    rows = [_config_stock_row(stock, prices.get(str(stock.get("symbol") or "").strip()) or {}, total_assets) for stock in stocks]
    cash = _safe_num(config.get("cash"))
    t2_pending_cash = _safe_num(config.get("t2_pending_cash"))
    try:
        from db.account_ledger import AccountLedger, REAL_ACCOUNT

        ledger = AccountLedger()
        ledger.ensure_initialized(config)
        summary = ledger.account_summary(REAL_ACCOUNT)
        cash = _safe_num(summary.get("cash_cny"), cash)
        t2_pending_cash = _safe_num(summary.get("t2_pending_cash_cny"), t2_pending_cash)
    except Exception:
        pass
    return {
        "version": 1,
        "generated_at": _file_timestamp(timestamp_path),
        "round_time": "config",
        "trading_mode": trading_mode_payload(config.get("trading_mode", DEFAULT_TRADING_MODE)),
        "monitor_action_email_enabled": _coerce_bool(config.get("monitor_action_email_enabled"), True),
        "cash_cny": round(cash, 2),
        "available_cash_cny": round(cash, 2),
        "t2_pending_cash_cny": round(t2_pending_cash, 2),
        "total_cash_cny": round(cash + t2_pending_cash, 2),
        "total_assets_cny": round(_safe_num(config.get("total_assets")), 2),
        "counts": {
            "symbols": len(rows),
            "action_required": 0,
            "entry_exit_alerts": 0,
            "suppressed": 0,
            "errors": 0 if rows else 1,
        },
        "rows": rows,
        "actionable": [],
        "entry_exit_alerts": [],
        "suppressed_alerts": [],
        "message": message,
        "source": "market_monitor_config",
    }


def _trade_panel_rows() -> List[Dict[str, Any]]:
    try:
        from jobs.market_monitor import load_config
        from db.account_ledger import AccountLedger, REAL_ACCOUNT

        config = load_config()
        ledger = AccountLedger()
        ledger.ensure_initialized(config)
        holding_by_symbol = {
            str(item.get("symbol") or ""): item
            for item in ledger.list_holdings(REAL_ACCOUNT)
        }
        merged: Dict[str, Dict[str, Any]] = {}
        for source_name, items in (
            ("持仓", config.get("holdings") or []),
            ("关注", config.get("watchlist") or []),
        ):
            for item in items:
                symbol = str(item.get("symbol") or "").strip()
                if not symbol:
                    continue
                row = merged.setdefault(
                    symbol,
                    {
                        "symbol": symbol,
                        "name": item.get("name") or symbol,
                        "source": source_name,
                        "units": 0.0,
                        "min_lot_size": int(_safe_num(item.get("min_lot_size"), 100) or 100),
                    },
                )
                row["name"] = item.get("name") or row["name"]
                row["source"] = "持仓" if source_name == "持仓" or row["source"] == "持仓" else "关注"
                holding = holding_by_symbol.get(symbol) or {}
                row["units"] = _safe_num(holding.get("units"))
                row["min_lot_size"] = int(
                    _safe_num(holding.get("min_lot_size"), row.get("min_lot_size") or item.get("min_lot_size") or 100)
                    or 100
                )
        return sorted(
            merged.values(),
            key=lambda row: (0 if _safe_num(row.get("units")) > 0 else 1, str(row.get("symbol") or "")),
        )
    except Exception as exc:  # noqa: BLE001
        return [{"symbol": "", "name": f"读取失败: {exc}", "source": "错误", "units": 0.0, "error": True}]


def _file_timestamp(path: Optional[Path]) -> Optional[str]:
    if path is None:
        return None
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
    except Exception:
        return None


def _path_mtime(path: Path) -> Optional[float]:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _find_latest_cached_committee_result(symbol: str) -> Optional[Dict[str, Any]]:
    import pickle
    try:
        cache_dir = ROOT / "data" / "committee_cache"
        if not cache_dir.exists():
            return None
        # Sort subdirectories YYYY-MM-DD descending
        date_dirs = sorted([d for d in cache_dir.iterdir() if d.is_dir()], key=lambda x: x.name, reverse=True)
        for date_dir in date_dirs:
            # Find any pkl matching *_<symbol>.pkl (ignoring case)
            matching_files = sorted(date_dir.glob(f"*_{symbol.upper()}.pkl"), key=lambda x: x.name, reverse=True)
            if matching_files:
                with open(matching_files[0], "rb") as f:
                    return pickle.load(f)
    except Exception:
        pass
    return None


def _positive_metric(value: Any) -> Optional[float]:
    number = _safe_num(value)
    return number if number > 0 else None


def _extract_tech_from_cached_result(cached: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Read technical fields already produced by the monitor/committee pipeline."""
    result: Dict[str, Any] = {"ma20": None, "ma120": None, "atr_pct": 0.0}
    if not isinstance(cached, dict):
        return result

    sources: List[Dict[str, Any]] = []
    for key in ("technical", "market_metrics", "metrics", "entry_exit_points"):
        value = cached.get(key)
        if isinstance(value, dict):
            sources.append(value)
    sources.append(cached)

    for field in ("ma20", "ma120"):
        for source in sources:
            metric = _positive_metric(source.get(field))
            if metric is not None:
                result[field] = metric
                break

    for source in sources:
        metric = _positive_metric(source.get("atr_pct"))
        if metric is not None:
            result["atr_pct"] = metric
            break

    return result


def _history_symbol_variants(symbol: str, market: str = "a") -> List[str]:
    variants = _symbol_variants(symbol)
    text = str(symbol or "").strip().upper()
    market_key = str(market or "").strip().lower()
    digits = "".join(ch for ch in text if ch.isdigit())
    if market_key in {"hk", "h", "hongkong"} and digits:
        variants.extend([digits, digits.zfill(5), f"{digits.zfill(5)}.HK"])
    out: List[str] = []
    for item in variants:
        if item and item not in out:
            out.append(item)
    return out


def _compute_tech_from_local_history(
    symbol: str,
    market: str = "a",
    *,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Compute MA/ATR from local SQLite cache only.

    This deliberately avoids the project history-data providers. The desktop
    window should display cached monitor state, not trigger network fetches or
    DB synchronization during UI refresh.
    """
    db = Path(db_path) if db_path is not None else MARKET_DB_PATH
    if not symbol or not db.exists():
        return {"ma20": None, "ma120": None, "atr_pct": 0.0, "regime_brief": ""}

    try:
        import sqlite3

        import pandas as pd

        from core.regime import format_regime_brief
        from utils.market_metrics import compute_metrics

        uri = f"file:{db.as_posix()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1.0) as conn:
            for variant in _history_symbol_variants(symbol, market):
                df = pd.read_sql_query(
                    (
                        "SELECT date AS Date, close AS Close, high AS High, "
                        "low AS Low, volume AS Volume FROM daily_prices "
                        "WHERE UPPER(symbol) = ? ORDER BY date ASC"
                    ),
                    conn,
                    params=(variant.upper(),),
                )
                if df.empty:
                    continue
                df["Date"] = pd.to_datetime(df["Date"])
                df = df.set_index("Date")
                for col in ("High", "Low", "Volume"):
                    if col in df.columns and df[col].isna().all():
                        df = df.drop(columns=[col])
                df = df.tail(180)
                if not df.empty:
                    m = compute_metrics(df)
                    return {
                        "ma20": _positive_metric(m.get("ma20")),
                        "ma120": _positive_metric(m.get("ma120")),
                        "atr_pct": _safe_num(m.get("atr_pct")),
                        "regime_brief": format_regime_brief(m, symbol=symbol),
                    }
    except Exception as exc:  # noqa: BLE001
        log.debug("local history technical fallback failed for %s: %s", symbol, exc)

    return {"ma20": None, "ma120": None, "atr_pct": 0.0, "regime_brief": ""}


def _tech_for_config_row(symbol: str, market: str, cached: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    tech = _extract_tech_from_cached_result(cached)
    cached_complete = (
        tech.get("ma20") is not None
        and tech.get("ma120") is not None
        and _safe_num(tech.get("atr_pct")) > 0
    )
    if cached_complete:
        return {**tech, "regime_brief": ""}

    local_tech = _compute_tech_from_local_history(symbol, market)
    local_complete = (
        local_tech.get("ma20") is not None
        and local_tech.get("ma120") is not None
        and _safe_num(local_tech.get("atr_pct")) > 0
    )

    if local_complete:
        return local_tech

    return {
        "ma20": tech.get("ma20") if tech.get("ma20") is not None else local_tech.get("ma20"),
        "ma120": tech.get("ma120") if tech.get("ma120") is not None else local_tech.get("ma120"),
        "atr_pct": _safe_num(tech.get("atr_pct")) if _safe_num(tech.get("atr_pct")) > 0 else _safe_num(local_tech.get("atr_pct")),
        "regime_brief": local_tech.get("regime_brief") or "",
    }




def _config_stock_row(stock: Dict[str, Any], price_info: Dict[str, Any], total_assets: float = 0.0) -> Dict[str, Any]:
    symbol = str(stock.get("symbol") or "").strip()
    position_pct = _safe_num(stock.get("position_pct"))
    units = _safe_num(stock.get("units"))
    cost = _safe_num(stock.get("cost"))
    if units <= 0.0 and position_pct > 0.0 and cost > 0.0 and total_assets > 0.0:
        units = (total_assets * position_pct / 100.0) / cost
    elif position_pct <= 0.0 and units > 0.0 and cost > 0.0 and total_assets > 0.0:
        position_pct = (units * cost) / total_assets * 100.0
    
    # Try to load cached committee result to populate indicators
    cached = _find_latest_cached_committee_result(symbol)
    
    buy_criteria = {"pullback_price": 0.0, "breakout_price": 0.0, "reentry_price": 0.0, "reward_risk_ratio": 0.0, "reason": ""}
    exit_points = {"stop_loss_price": 0.0, "take_profit_price": 0.0, "trim_price": 0.0}
    verdict = "HOLD" if position_pct > 0 else "WAIT"
    confidence = 0.0
    suggested_alloc_cny = 0.0
    
    fundamental_score = 50.0
    technical_regime = "等待交易时段监控刷新"
    technical_quant_view = "当前显示本地持仓配置"
    
    if cached:
        # Populate buy_criteria
        ee = cached.get("entry_exit_points") or {}
        buy_criteria["pullback_price"] = _safe_num(ee.get("buy_pullback_price"))
        buy_criteria["breakout_price"] = _safe_num(ee.get("buy_breakout_price"))
        buy_criteria["reentry_price"] = _safe_num(ee.get("reentry_price"))
        buy_criteria["reward_risk_ratio"] = _safe_num(ee.get("reward_risk_ratio"))
        buy_criteria["reason"] = ee.get("reason", "")
        
        # Populate exit_points
        exit_points["stop_loss_price"] = _safe_num(ee.get("stop_loss_price"))
        exit_points["take_profit_price"] = _safe_num(ee.get("take_profit_price"))
        exit_points["trim_price"] = _safe_num(ee.get("trim_price"))
            
        verdict = cached.get("verdict") or verdict
        confidence = _safe_num(cached.get("confidence"))
        suggested_alloc_cny = _safe_num(cached.get("suggested_alloc"))
        
        fundamental_score = _safe_num(cached.get("fundamental_score"), 50.0)
        technical_regime = cached.get("regime") or technical_regime
        technical_quant_view = cached.get("quant_signal") or technical_quant_view

    tech = _tech_for_config_row(symbol, stock.get("market", "a"), cached)
    atr_pct = _safe_num(tech.get("atr_pct"))
    if tech.get("regime_brief") and (
        not technical_regime
        or technical_regime == "等待交易时段监控刷新"
        or "缺少必要指标" in technical_regime
    ):
        technical_regime = str(tech.get("regime_brief"))
    if tech.get("regime_brief") and (
        not technical_quant_view
        or technical_quant_view == "当前显示本地持仓配置"
        or "数据缺失" in technical_quant_view
        or "多周期数据缺失" in technical_quant_view
    ):
        technical_quant_view = "已根据本地历史K线补齐均线与波动率；更完整买卖点结论等待下一轮委员会分析刷新。"

    return {

        "symbol": symbol,
        "name": stock.get("name") or price_info.get("name") or symbol,
        "market": stock.get("market", "a"),
        "sector": stock.get("sector", ""),
        "industry": stock.get("industry", ""),
        "min_lot_size": int(_safe_num(stock.get("min_lot_size"), 100) or 100),
        "units": round(units, 4),
        "is_holding": position_pct > 0 or units > 0,
        "position_pct": round(position_pct, 4),
        "target_position_pct": stock.get("target_position_pct", stock.get("target_pct")),
        "cost": _safe_num(stock.get("cost")),
        "price": {
            "current": _safe_num(price_info.get("price")),
            "prev_close": _safe_num(price_info.get("prev_close")),
            "change_pct": _safe_num(price_info.get("change_pct")),
            "name": price_info.get("name", ""),
        },
        "state": "monitoring" if position_pct > 0 else "candidate",
        "buy_criteria": buy_criteria,
        "exit_points": exit_points,
        "fundamental": {"model": "", "score": fundamental_score, "coverage": 0.0, "anchor_multiplier": 1.0},
        "technical": {
            "regime": technical_regime,
            "quant_view": technical_quant_view,
            "market_data_excerpt": "",
            "entry_exit_model": "",
            "low_confidence": True,
            "ma20": tech.get("ma20"),
            "ma120": tech.get("ma120"),
            "atr_pct": atr_pct,
            "expected_return_pct": 0.0,
        },
        "operation": {
            "status": "monitoring" if position_pct > 0 else "candidate",
            "reason": "local_config_snapshot",
            "verdict": verdict,
            "confidence": confidence,
            "suggested_alloc_cny": suggested_alloc_cny,
            "optimizer_lots": None,
            "llm_review_lots": None,
            "llm_position_scale": "",
            "llm_position_scale_value": None,
            "llm_position_scale_multiplier": None,
            "llm_risk_components": {},
            "alert_score": 0.0,
            "triggers": [],
            "confirmed": False,
            "llm_conflict": False,
            "execution_blocked": False,
        },
        "llm_review": {"conclusion": "", "one_line": "从本地缓存载入上一次委员会分析结果", "risk_note": "", "execution_plan": "", "raw_excerpt": ""},
        "suppressed_reasons": [],
        "error": "",
        "success": True,
    }


def _load_weekend_news_cards(news_dir: Path = WEEKEND_NEWS_DIR) -> tuple[List[Dict[str, Any]], str]:
    candidates = sorted(
        [*news_dir.glob("summary_*.json"), *news_dir.glob("report_*.json")],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else payload
        sectors = summary.get("sector_opportunities") or []
        hot_stocks = summary.get("hot_stock_opportunities") or []
        cards: List[Dict[str, Any]] = []
        for sector in sectors:
            leaders = sector.get("leaders") or []
            cards.append(
                {
                    "title": sector.get("theme") or sector.get("sector") or "周末机会",
                    "sector": sector.get("sector") or "-",
                    "logic": sector.get("logic") or summary.get("summary_one_liner") or "-",
                    "heat_score": _safe_num(sector.get("heat_score")),
                    "freshness_score": _safe_num(sector.get("freshness_score")),
                    "leaders": leaders,
                    "risk_note": "；".join(
                        str(leader.get("risk_note") or "").strip()
                        for leader in leaders
                        if leader.get("risk_note")
                    ),
                }
            )
        if not cards:
            for stock in hot_stocks:
                cards.append(
                    {
                        "title": stock.get("theme") or stock.get("sector") or "周末机会",
                        "sector": stock.get("sector") or "-",
                        "logic": stock.get("reason") or summary.get("summary_one_liner") or "-",
                        "heat_score": _safe_num(stock.get("score")),
                        "freshness_score": 0.0,
                        "leaders": [stock],
                        "risk_note": stock.get("risk_note") or "",
                    }
                )
        if cards:
            return _sort_news_cards(cards), path.name
    return [], "暂无包含板块与龙头股的周末新闻总结"


def _load_daily_selection(path: Path = DAILY_SELECTION_LATEST) -> Dict[str, Any]:
    if not path.exists():
        return {"stocks": [], "message": "暂无日度选股快照"}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"stocks": [], "message": f"读取日度选股失败: {type(exc).__name__}: {exc}"}


def _stock_priority_key(row: Dict[str, Any]) -> tuple[Any, ...]:
    state = str(row.get("state") or "")
    operation = row.get("operation") or {}
    verdict = str(operation.get("verdict") or "").upper()
    status = str(operation.get("status") or state)
    alert_score = _safe_num(operation.get("alert_score"))
    confirmed = bool(operation.get("confirmed"))
    allocation = abs(_safe_num(operation.get("suggested_alloc_cny")))
    change = abs(_safe_num((row.get("price") or {}).get("change_pct")))
    return (
        STATE_PRIORITY.get(state, 99),
        STATE_PRIORITY.get(status, 99),
        -int(confirmed),
        -alert_score,
        VERDICT_PRIORITY.get(verdict, 99),
        -allocation,
        -change,
        str(row.get("symbol") or ""),
    )


def _sort_stock_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(rows, key=_stock_priority_key)


def _news_impact_score(card: Dict[str, Any]) -> float:
    leaders = card.get("leaders") or []
    leader_confidence = max(
        (_safe_num(leader.get("confidence")) for leader in leaders),
        default=0.0,
    )
    breadth = min(len(leaders), 4) / 4
    heat = _safe_num(card.get("heat_score"))
    freshness = _safe_num(card.get("freshness_score"))
    return heat * 0.55 + freshness * 0.25 + leader_confidence * 0.15 + breadth * 0.05


def _sort_news_cards(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        cards,
        key=lambda card: (
            -_news_impact_score(card),
            -_safe_num(card.get("heat_score")),
            str(card.get("title") or ""),
        ),
    )


def _demo_rows() -> List[Dict[str, Any]]:
    return [
        {
            "symbol": "600519",
            "name": "贵州茅台",
            "sector": "白酒",
            "state": "monitoring",
            "price": {"current": 1418.20, "change_pct": 1.26},
            "fundamental": {"score": 91},
            "technical": {"regime": "日线企稳，周线震荡"},
            "buy_criteria": {"pullback_price": 1380, "breakout_price": 1435, "reward_risk_ratio": 2.1},
            "exit_points": {"stop_loss_price": 1348, "take_profit_price": 1508},
            "operation": {"verdict": "WAIT", "status": "monitoring", "suggested_alloc_cny": 0},
            "min_lot_size": 100,
            "units": 0,
            "_demo": True,
        },
        {
            "symbol": "603308",
            "name": "应流股份",
            "sector": "商业航天",
            "state": "action_required",
            "price": {"current": 28.64, "change_pct": 4.82},
            "fundamental": {"score": 76},
            "technical": {"regime": "日线突破，周线转强"},
            "buy_criteria": {"pullback_price": 27.90, "breakout_price": 28.50, "reward_risk_ratio": 2.8},
            "exit_points": {"stop_loss_price": 26.80, "take_profit_price": 32.20},
            "operation": {"verdict": "BUY", "status": "action_required", "suggested_alloc_cny": 5728},
            "min_lot_size": 100,
            "units": 200,
            "_demo": True,
        },
    ]


def _demo_news_cards() -> List[Dict[str, Any]]:
    return [
        {
            "title": "商业航天融资升温",
            "sector": "商业航天",
            "logic": "海外商业航天融资与上市预期升温，卫星制造、航天材料和高端铸件可能获得主题催化。",
            "heat_score": 0.86,
            "freshness_score": 0.91,
            "leaders": [{"symbol": "603308", "name": "应流股份"}, {"symbol": "300455", "name": "航天智装"}],
            "risk_note": "海外事件到A股映射存在兑现风险，避免高开追涨。",
        },
        {
            "title": "算电协同政策催化",
            "sector": "算力基础设施",
            "logic": "算力与电力协同政策推动数据中心能源效率升级，电网设备及算力基础设施值得跟踪。",
            "heat_score": 0.78,
            "freshness_score": 0.84,
            "leaders": [{"symbol": "300001", "name": "特锐德"}, {"symbol": "600406", "name": "国电南瑞"}],
            "risk_note": "政策落地节奏与订单兑现仍需验证。",
        },
    ]


def _demo_selection_payload() -> Dict[str, Any]:
    return {
        "trade_date": "2026-06-13",
        "generated_at": "演示数据",
        "stocks": [
            {
                "symbol": "603308",
                "name": "应流股份",
                "sector": "商业航天",
                "score": 86.2,
                "attention": "priority_watch",
                "reasons": ["商业航天热度提升，高端铸件供应链映射清晰", "日线突破，周线转强"],
                "money_flow_score": 88,
                "fundamental_score": 76,
                "evidence_titles": ["商业航天融资升温"],
                "tape": {"interpretation": "放量突破 20 日高点，收盘位置强", "change_pct": 4.82, "tape_score": 82.0},
                "trend": {"interpretation": "日线、周线偏强，月线修复中", "alignment": "partial_bullish"},
                "entry_plan": {"action": "buy_breakout", "trigger_price": 28.5, "stop_loss_price": 26.8, "note": "突破价上方缩量回踩不破可关注"},
            },
            {
                "symbol": "300001",
                "name": "特锐德",
                "sector": "算力基础设施",
                "score": 78.4,
                "attention": "watch",
                "reasons": ["算电协同政策催化，充电网和能源基础设施映射", "日线强于板块但追高风险中等"],
                "money_flow_score": 72,
                "fundamental_score": 68,
                "evidence_titles": ["算电协同政策催化"],
                "tape": {"interpretation": "温和放量上行，未出现明显破位", "change_pct": 2.31, "tape_score": 71.0},
                "trend": {"interpretation": "日线向上，周线震荡，月线未完全确认", "alignment": "mixed"},
                "entry_plan": {"action": "wait_pullback", "trigger_price": 21.2, "stop_loss_price": 19.6, "note": "等待回踩均线区间企稳，不追高"},
            },
        ],
    }


def _run_latest_committee_for_row(row: Dict[str, Any]) -> Dict[str, Any]:
    from jobs.market_monitor import (
        _sync_config_account_fields,
        apply_limit_up_guard,
        apply_repeated_trade_guard,
        build_holdings_list,
        call_committee,
        fetch_sina_prices,
        load_entry_exit_alert_state,
        load_config,
    )

    symbol = str(row.get("symbol") or "").upper()
    if not symbol:
        return {"success": False, "symbol": symbol, "error": "缺少标的代码"}

    config = load_config()
    shadow_stocks: List[Dict[str, Any]] = []
    shadow_cash = 0.0
    shadow_t2_pending = 0.0
    try:
        from db.account_ledger import AccountLedger, COMMITTEE_ACCOUNT, REAL_ACCOUNT

        ledger = AccountLedger()
        ledger.ensure_initialized(config)
        account_stocks = ledger.stocks_for_committee_input(config, account=REAL_ACCOUNT)
        config = _sync_config_account_fields(config, account_stocks)
        shadow_stocks = ledger.stocks_for_committee_input(config, account=COMMITTEE_ACCOUNT)
        shadow_summary = ledger.account_summary(COMMITTEE_ACCOUNT)
        shadow_cash = _safe_num(shadow_summary.get("cash_cny"))
        shadow_t2_pending = _safe_num(shadow_summary.get("t2_pending_cash_cny"))
    except Exception:
        ledger = None

    holdings = config.get("holdings", [])
    watchlist = config.get("watchlist", [])
    stocks = holdings + watchlist
    stock = next((s for s in stocks if str(s.get("symbol", "")).upper() == symbol), None)
    if not stock:
        stock = {
            "symbol": symbol,
            "name": row.get("name", symbol),
            "market": row.get("market", "a"),
            "position_pct": row.get("position_pct", 0),
            "cost": row.get("cost", 0),
            "sector": row.get("sector", ""),
            "industry": row.get("industry", ""),
        }

    total_assets = float(config.get("total_assets", 0) or 0)
    cash = float(config.get("cash", 0) or 0)
    t2_pending_cash = float(config.get("t2_pending_cash", 0) or 0)
    if ledger is not None:
        try:
            real_summary = ledger.account_summary("real")
            cash = float(real_summary.get("cash_cny", cash) or cash)
            t2_pending_cash = float(real_summary.get("t2_pending_cash_cny", t2_pending_cash) or 0)
        except Exception:
            pass

    price_info = fetch_sina_prices([symbol]).get(symbol) or {}
    current_price = _safe_num(
        price_info.get("price"),
        _safe_num((row.get("price") or {}).get("current")),
    )
    other_holdings = build_holdings_list(holdings, symbol)
    shadow_stock = next(
        (s for s in shadow_stocks if str(s.get("symbol") or "").upper() == symbol),
        None,
    )
    try:
        from jobs.market_monitor_quotes import build_behavioral_factor_context
        behavioral_factor = build_behavioral_factor_context(stocks).get(symbol.upper())
    except Exception:
        behavioral_factor = None
    result = call_committee(
        symbol=symbol,
        name=str(stock.get("name") or row.get("name") or symbol),
        market=str(stock.get("market") or row.get("market") or "a"),
        position_pct=_safe_num(stock.get("position_pct")),
        cost=_safe_num(stock.get("cost")),
        current_price=current_price,
        total_assets=total_assets,
        cash=cash,
        all_holdings=other_holdings,
        target_position_pct=stock.get("target_position_pct", stock.get("target_pct")),
        t2_pending=t2_pending_cash,
        sector=str(stock.get("sector") or row.get("sector") or ""),
        industry=str(stock.get("industry") or row.get("industry") or ""),
        fundamentals=stock.get("fundamentals", {}),
        optimizer_review_enabled=True,
        shadow_position_pct=_safe_num((shadow_stock or {}).get("position_pct")),
        shadow_cost=_safe_num((shadow_stock or {}).get("cost")),
        shadow_cash=shadow_cash,
        shadow_holdings=build_holdings_list(shadow_stocks, symbol),
        shadow_available_cash=shadow_cash,
        shadow_t2_pending=shadow_t2_pending,
        behavioral_factor=behavioral_factor,
    ) or {"success": False, "symbol": symbol, "error": "委员会返回空结果"}

    if not result.get("success"):
        return result
    result.setdefault("symbol", symbol)
    result.setdefault("name", stock.get("name") or row.get("name") or symbol)
    result.setdefault("market", stock.get("market") or row.get("market") or "a")
    result.setdefault("sector", stock.get("sector") or row.get("sector") or "")
    result.setdefault("industry", stock.get("industry") or row.get("industry") or "")
    entry_exit_state = load_entry_exit_alert_state()
    result = apply_repeated_trade_guard(
        result,
        stock=stock,
        current_price=current_price,
        ledger=ledger,
        entry_exit_state=entry_exit_state,
        account="real",
    )
    result = apply_limit_up_guard(result, price_info=price_info)

    if ledger is None:
        return result
    analysis_id = ledger.new_analysis_id(symbol, "desktop")
    ledger.record_decision(result, account="real", analysis_id=analysis_id)

    shadow_result = result.get("shadow_result")
    if isinstance(shadow_result, dict) and shadow_result.get("success"):
        shadow_result.setdefault("symbol", symbol)
        shadow_result.setdefault("name", stock.get("name") or row.get("name") or symbol)
        shadow_result.setdefault("market", stock.get("market") or row.get("market") or "a")
        shadow_result = apply_repeated_trade_guard(
            shadow_result,
            stock=shadow_stock or stock,
            current_price=current_price,
            ledger=ledger,
            entry_exit_state={},
            account="committee",
        )
        shadow_result = apply_limit_up_guard(shadow_result, price_info=price_info)
        ledger.record_decision(shadow_result, account="committee", analysis_id=analysis_id)
        ledger.apply_committee_result(shadow_result, price=current_price)
        result["shadow_result"] = shadow_result
    return result




__all__ = [
    "_safe_num",
    "_remote_api_headers",
    "_stop_background_services",
    "_fmt_price",
    "_fmt_money",
    "_fmt_cash_line",
    "_fill_missing_row_sectors",
    "_correct_real_cash",
    "_current_trading_mode",
    "_coerce_bool",
    "_current_action_email_enabled",
    "_set_action_email_enabled",
    "_set_trading_mode",
    "_cash_ratio",
    "_fmt_update_time",
    "_fmt_lots",
    "_fmt_holding_lots",
    "_max_executable_lots",
    "_suggested_lots",
    "_lot_size",
    "_operation_direction",
    "_execute_user_trade_from_row",
    "_execute_user_trade_manual",
    "_is_config_watch_only",
    "_remove_from_watchlist",
    "_filter_rows_by_current_config",
    "_fmt_pct",
    "_short",
    "_load_snapshot",
    "_write_snapshot",
    "_recount_snapshot_rows",
    "_merge_snapshot_row",
    "_update_snapshot_row",
    "_looks_like_sample_snapshot",
    "_load_config_snapshot",
    "_trade_panel_rows",
    "_file_timestamp",
    "_path_mtime",
    "_config_stock_row",
    "_load_weekend_news_cards",
    "_load_daily_selection",
    "_stock_priority_key",
    "_sort_stock_rows",
    "_news_impact_score",
    "_sort_news_cards",
    "_demo_rows",
    "_demo_news_cards",
    "_demo_selection_payload",
    "_run_latest_committee_for_row",
]
