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
- `fetch_prices(symbols: List[str])`: Fetches real-time price info.
- `get_history_data(symbol: str, period: str, as_of_date: Optional[str])`: Automatically routes requests to `akshare_data.py` (for A-Shares) and `exchange_fee.py` (for global/US stocks). It handles `as_of_date` filtering to protect backtests from lookahead bias.
- `search_symbols(query: str, limit: int)`: Resolves query strings into stock codes and names.
- `get_macro_snapshot(as_of_date: Optional[str])`: Aggregates key domestic macro indicators.
