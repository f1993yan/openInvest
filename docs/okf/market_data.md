---
type: Reference
title: openInvest Unified Market Data Layer
description: Specifications of the unified intermediate data provider layer utils/market_data_provider.py.
resource: ../../utils/market_data_provider.py
tags: [market-data, akshare, yfinance, connector]
timestamp: 2026-06-21T20:46:00Z
---

# Unified Market Data Layer

To facilitate seamless switching of external data sources, openInvest isolates all stock prices, historical daily bars, symbol lookups, and macro indices into a unified intermediate layer: `utils/market_data_provider.py`.

## Core API Interfaces
- `fetch_prices(symbols: List[str])`: Fetches real-time price info. A-share and HK quotes prefer Tencent batch quotes and fall back to Sina when Tencent is unavailable.
- `get_history_data(symbol: str, period: str, as_of_date: Optional[str])`: Normalizes A-share/HK symbols and routes them to `akshare_data.py`; global/US/FX assets fall back to `exchange_fee.py` / yfinance. It handles `as_of_date` filtering to protect backtests from lookahead bias.
- `search_symbols(query: str, limit: int)`: Resolves query strings into stock codes and names.
- `get_macro_snapshot(as_of_date: Optional[str])`: Aggregates key domestic macro indicators.

## Routing Notes
- A-share daily bars are fetched in this order: Eastmoney JSON API, Tencent K-line API, then AkShare/Sina as the last fallback.
- HK daily bars prefer AkShare and then `utils.cn_market_provider`.
- The Windows desktop bootstrap prepends the project root to `PYTHONPATH` so the local `py_mini_racer` stub prevents the real V8 DLL from crashing the process. Data paths that need JavaScript execution should fail visibly instead of silently emptying the selection pool.
