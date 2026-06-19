import sqlite3
import json
import os
import sys
import pandas as pd
import numpy as np

# Ensure project root is in path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

# Helper function to compute RSI series
from utils.chan import _calc_rsi_series

PARAMS_PATH = os.path.join(project_root, "data", "chan_patterns_params.json")
MARKET_DB = os.path.join(project_root, "db", "market_data.db")
ACCOUNTS_DB = os.path.join(project_root, "db", "accounts.db")

def get_holdings():
    if not os.path.exists(ACCOUNTS_DB):
        return []
    conn = sqlite3.connect(ACCOUNTS_DB)
    cur = conn.cursor()
    try:
        cur.execute("SELECT symbol, name FROM holdings WHERE account = 'real' AND units > 0")
        res = cur.fetchall()
    except Exception:
        res = []
    finally:
        conn.close()
    return res

def evaluate_gap_params(df: pd.DataFrame, symbol: str, threshold: float, trend_w: int) -> tuple:
    closes = df['Close'].tolist()
    highs = df['High'].tolist()
    lows = df['Low'].tolist()
    dates = df.index.tolist()
    n = len(closes)

    ma_trend = df['Close'].rolling(window=trend_w).mean().tolist()

    total = 0
    correct = 0

    # We evaluate from n-65 to n-5 (similar to the validation setup)
    for i in range(max(trend_w, n - 65), n - 5):
        prev_close = closes[i-1]
        prev_high = highs[i-1]
        prev_low = lows[i-1]
        curr_high = highs[i]
        curr_low = lows[i]
        curr_close = closes[i]

        outcome_close = closes[i+5]

        # Upward gap
        gap_limit_up = prev_close * threshold
        if curr_low > prev_high + gap_limit_up:
            if ma_trend[i] is not None and not pd.isna(ma_trend[i]) and curr_close > ma_trend[i]:
                total += 1
                if outcome_close > curr_close:
                    correct += 1

        # Downward gap
        gap_limit_down = prev_close * threshold
        if curr_high < prev_low - gap_limit_down:
            if ma_trend[i] is not None and not pd.isna(ma_trend[i]) and curr_close < ma_trend[i]:
                total += 1
                if outcome_close < curr_close:
                    correct += 1

    return correct, total

def evaluate_failed_limit_up_params(df: pd.DataFrame, symbol: str, name: str, vol_mult: float, shadow_pct: float, trend_w: int) -> tuple:
    closes = df['Close'].tolist()
    highs = df['High'].tolist()
    volumes = df['Volume'].tolist() if 'Volume' in df.columns else None
    dates = df.index.tolist()
    n = len(closes)

    vol_ma20 = df['Volume'].rolling(window=20).mean().tolist() if 'Volume' in df.columns else None
    ma_trend = df['Close'].rolling(window=trend_w).mean().tolist()

    # A-share limit percentage
    sym = (symbol or "").strip()
    limit_pct = 10.0
    if sym.startswith(("300", "301", "688", "689")):
        limit_pct = 20.0
    elif sym.startswith(("8", "4")):
        limit_pct = 30.0
    is_st = "ST" in (name or "").upper()
    if is_st and limit_pct == 10.0:
        limit_pct = 5.0

    total = 0
    correct = 0

    for i in range(max(trend_w, n - 65), n - 5):
        prev_close = closes[i-1]
        curr_high = highs[i]
        curr_close = closes[i]
        outcome_close = closes[i+5]

        limit_price = round(prev_close * (1 + limit_pct / 100.0), 2)
        touched = curr_high >= limit_price - 0.005
        failed_to_close = curr_close < limit_price - 0.005

        if touched and failed_to_close:
            vol_ok = True
            if volumes is not None and vol_ma20 is not None and vol_ma20[i] is not None and not pd.isna(vol_ma20[i]):
                vol_ok = volumes[i] >= vol_ma20[i] * vol_mult

            shadow_ok = (curr_high - curr_close) / curr_close >= shadow_pct

            not_too_strong = True
            if ma_trend[i] is not None and not pd.isna(ma_trend[i]):
                if curr_close > ma_trend[i] * 1.15:
                    not_too_strong = False

            if vol_ok and shadow_ok and not_too_strong:
                total += 1
                if outcome_close < curr_close:
                    correct += 1

    return correct, total

def evaluate_rsi_divergence_params(df: pd.DataFrame, rsi_ob: float, rsi_os: float) -> tuple:
    closes = df['Close'].tolist()
    dates = df.index.tolist()
    n = len(closes)

    rsi_series = _calc_rsi_series(closes, period=14)
    total = 0
    correct = 0

    # We evaluate sequentially
    for i in range(35, n - 5):
        outcome_close = closes[i+5]

        # We need a sub-slice to detect divergence at index i
        df_sub = df.iloc[:i+1]
        sub_closes = df_sub['Close'].tolist()
        sub_rsi = rsi_series[:i+1]

        window = min(40, len(sub_closes))
        recent_closes = sub_closes[-window:]
        recent_rsi = sub_rsi[-window:]

        peaks = []
        valleys = []
        for k in range(1, window - 1):
            if recent_rsi[k] is None:
                continue
            if recent_closes[k] > recent_closes[k-1] and recent_closes[k] > recent_closes[k+1]:
                peaks.append({'idx': k, 'price': recent_closes[k], 'rsi': recent_rsi[k]})
            if recent_closes[k] < recent_closes[k-1] and recent_closes[k] < recent_closes[k+1]:
                valleys.append({'idx': k, 'price': recent_closes[k], 'rsi': recent_rsi[k]})

        # Bearish divergence at the last bar (index window-1)
        if len(peaks) >= 2:
            p1 = peaks[-2]
            p2 = peaks[-1]
            if p2['idx'] == window - 2: # Triggered on the peak day
                price_rising = p2['price'] > p1['price'] * 1.01
                rsi_falling = p2['rsi'] < p1['rsi'] - 2.0
                rsi_ob_check = (p1['rsi'] > rsi_ob or p2['rsi'] > rsi_ob)
                if price_rising and rsi_falling and rsi_ob_check:
                    total += 1
                    if outcome_close < sub_closes[-1]:
                        correct += 1

        # Bullish divergence
        if len(valleys) >= 2:
            v1 = valleys[-2]
            v2 = valleys[-1]
            if v2['idx'] == window - 2:
                price_falling = v2['price'] < v1['price'] * 0.99
                rsi_rising = v2['rsi'] > v1['rsi'] + 2.0
                rsi_os_check = (v1['rsi'] < rsi_os or v2['rsi'] < rsi_os)
                if price_falling and rsi_rising and rsi_os_check:
                    total += 1
                    if outcome_close > sub_closes[-1]:
                        correct += 1

    return correct, total

def optimize_symbol(symbol: str, name: str, df: pd.DataFrame) -> dict:
    best_params = {}

    # 1. Optimize Gaps
    gap_choices = [0.001, 0.003, 0.005, 0.010]
    trend_choices = [20, 60]

    best_gap_pct = 0.003
    best_trend_w = 60
    best_gap_score = -1.0
    best_gap_triggers = 0

    for pct in gap_choices:
        for tw in trend_choices:
            corr, tot = evaluate_gap_params(df, symbol, pct, tw)
            if tot > 0:
                acc = corr / tot
                # Selection rule: higher accuracy, then more triggers to avoid overfitting
                if (acc > best_gap_score) or (acc == best_gap_score and tot > best_gap_triggers):
                    best_gap_score = acc
                    best_gap_triggers = tot
                    best_gap_pct = pct
                    best_trend_w = tw

    best_params['gap_threshold_pct'] = best_gap_pct
    best_params['trend_window'] = best_trend_w

    # 2. Optimize Failed Limit Up
    vol_choices = [1.0, 1.2, 1.5]
    shadow_choices = [0.01, 0.02, 0.03]

    best_vol_mult = 1.2
    best_shadow_pct = 0.02
    best_flu_score = -1.0
    best_flu_triggers = 0

    for vm in vol_choices:
        for sp in shadow_choices:
            # We reuse the best_trend_w from gap optimization to keep it consistent
            corr, tot = evaluate_failed_limit_up_params(df, symbol, name, vm, sp, best_trend_w)
            if tot > 0:
                acc = corr / tot
                if (acc > best_flu_score) or (acc == best_flu_score and tot > best_flu_triggers):
                    best_flu_score = acc
                    best_flu_triggers = tot
                    best_vol_mult = vm
                    best_shadow_pct = sp

    best_params['failed_limit_up_vol_mult'] = best_vol_mult
    best_params['failed_limit_up_shadow_pct'] = best_shadow_pct

    # 3. Optimize RSI Divergence
    rsi_ob_choices = [60.0, 65.0, 70.0]
    rsi_os_choices = [30.0, 35.0, 40.0]

    best_ob = 65.0
    best_os = 35.0
    best_rsi_score = -1.0
    best_rsi_triggers = 0

    for ob in rsi_ob_choices:
        for os in rsi_os_choices:
            corr, tot = evaluate_rsi_divergence_params(df, ob, os)
            if tot > 0:
                acc = corr / tot
                if (acc > best_rsi_score) or (acc == best_rsi_score and tot > best_rsi_triggers):
                    best_rsi_score = acc
                    best_rsi_triggers = tot
                    best_ob = ob
                    best_os = os

    best_params['rsi_overbought'] = best_ob
    best_params['rsi_oversold'] = best_os

    best_params['accuracy_meta'] = get_accuracy_meta(df, symbol, name, best_params)
    return best_params

def get_accuracy_meta(df: pd.DataFrame, symbol: str, name: str, best_params: dict) -> dict:
    closes = df['Close'].tolist()
    highs = df['High'].tolist()
    lows = df['Low'].tolist()
    volumes = df['Volume'].tolist() if 'Volume' in df.columns else None
    dates = df.index.tolist()
    n = len(closes)

    gap_pct = best_params['gap_threshold_pct']
    trend_w = best_params['trend_window']
    vol_mult = best_params['failed_limit_up_vol_mult']
    shadow_pct = best_params['failed_limit_up_shadow_pct']
    rsi_ob = best_params['rsi_overbought']
    rsi_os = best_params['rsi_oversold']

    ma_trend = df['Close'].rolling(window=trend_w).mean().tolist()
    vol_ma20 = df['Volume'].rolling(window=20).mean().tolist() if 'Volume' in df.columns else None
    rsi_series = _calc_rsi_series(closes, period=14)

    # A-share limit percentage
    sym = (symbol or "").strip()
    limit_pct = 10.0
    if sym.startswith(("300", "301", "688", "689")):
        limit_pct = 20.0
    elif sym.startswith(("8", "4")):
        limit_pct = 30.0
    is_st = "ST" in (name or "").upper()
    if is_st and limit_pct == 10.0:
        limit_pct = 5.0

    meta = {
        'upward_gap': {'correct': 0, 'total': 0, 'accuracy': 0.0},
        'downward_gap': {'correct': 0, 'total': 0, 'accuracy': 0.0},
        'failed_limit_up': {'correct': 0, 'total': 0, 'accuracy': 0.0},
        'rsi_bearish': {'correct': 0, 'total': 0, 'accuracy': 0.0},
        'rsi_bullish': {'correct': 0, 'total': 0, 'accuracy': 0.0},
    }

    # Evaluate sequentially over the last 60 trading days
    for i in range(max(trend_w, n - 65), n - 5):
        prev_close = closes[i-1]
        prev_high = highs[i-1]
        prev_low = lows[i-1]
        curr_high = highs[i]
        curr_low = lows[i]
        curr_close = closes[i]

        outcome_close = closes[i+5]

        # Upward gap
        gap_limit = prev_close * gap_pct
        if curr_low > prev_high + gap_limit:
            if ma_trend[i] is not None and not pd.isna(ma_trend[i]) and curr_close > ma_trend[i]:
                meta['upward_gap']['total'] += 1
                if outcome_close > curr_close:
                    meta['upward_gap']['correct'] += 1

        # Downward gap
        if curr_high < prev_low - gap_limit:
            if ma_trend[i] is not None and not pd.isna(ma_trend[i]) and curr_close < ma_trend[i]:
                meta['downward_gap']['total'] += 1
                if outcome_close < curr_close:
                    meta['downward_gap']['correct'] += 1

        # Failed limit up
        limit_price = round(prev_close * (1 + limit_pct / 100.0), 2)
        touched = curr_high >= limit_price - 0.005
        failed_to_close = curr_close < limit_price - 0.005

        if touched and failed_to_close:
            vol_ok = True
            if volumes is not None and vol_ma20 is not None and vol_ma20[i] is not None and not pd.isna(vol_ma20[i]):
                vol_ok = volumes[i] >= vol_ma20[i] * vol_mult
            shadow_ok = (curr_high - curr_close) / curr_close >= shadow_pct
            not_too_strong = True
            if ma_trend[i] is not None and not pd.isna(ma_trend[i]):
                if curr_close > ma_trend[i] * 1.15:
                    not_too_strong = False
            if vol_ok and shadow_ok and not_too_strong:
                meta['failed_limit_up']['total'] += 1
                if outcome_close < curr_close:
                    meta['failed_limit_up']['correct'] += 1

        # RSI Divergence
        df_sub = df.iloc[:i+1]
        sub_closes = df_sub['Close'].tolist()
        sub_rsi = rsi_series[:i+1]
        window = min(40, len(sub_closes))
        recent_closes = sub_closes[-window:]
        recent_rsi = sub_rsi[-window:]

        peaks = []
        valleys = []
        for k in range(1, window - 1):
            if recent_rsi[k] is None:
                continue
            if recent_closes[k] > recent_closes[k-1] and recent_closes[k] > recent_closes[k+1]:
                peaks.append({'idx': k, 'price': recent_closes[k], 'rsi': recent_rsi[k]})
            if recent_closes[k] < recent_closes[k-1] and recent_closes[k] < recent_closes[k+1]:
                valleys.append({'idx': k, 'price': recent_closes[k], 'rsi': recent_rsi[k]})

        # Bearish divergence
        if len(peaks) >= 2:
            p1 = peaks[-2]
            p2 = peaks[-1]
            if p2['idx'] == window - 2:
                price_rising = p2['price'] > p1['price'] * 1.01
                rsi_falling = p2['rsi'] < p1['rsi'] - 2.0
                rsi_ob_check = (p1['rsi'] > rsi_ob or p2['rsi'] > rsi_ob)
                if price_rising and rsi_falling and rsi_ob_check:
                    meta['rsi_bearish']['total'] += 1
                    if outcome_close < sub_closes[-1]:
                        meta['rsi_bearish']['correct'] += 1

        # Bullish divergence
        if len(valleys) >= 2:
            v1 = valleys[-2]
            v2 = valleys[-1]
            if v2['idx'] == window - 2:
                price_falling = v2['price'] < v1['price'] * 0.99
                rsi_rising = v2['rsi'] > v1['rsi'] + 2.0
                rsi_os_check = (v1['rsi'] < rsi_os or v2['rsi'] < rsi_os)
                if price_falling and rsi_rising and rsi_os_check:
                    meta['rsi_bullish']['total'] += 1
                    if outcome_close > sub_closes[-1]:
                        meta['rsi_bullish']['correct'] += 1

    # Calculate percentage
    for k in meta.keys():
        tot = meta[k]['total']
        corr = meta[k]['correct']
        meta[k]['accuracy'] = round((corr / tot * 100.0), 1) if tot > 0 else 0.0

    return meta


def main():
    print("Starting weekly parameter optimization...")
    holdings = get_holdings()
    if not holdings:
        print("No active holdings found in accounts.db.")
        return

    print(f"Loaded {len(holdings)} holdings. Optimizing parameters...")

    if not os.path.exists(MARKET_DB):
        print(f"Market database not found at {MARKET_DB}")
        return

    conn = sqlite3.connect(MARKET_DB)

    # Load existing params
    params = {}
    if os.path.exists(PARAMS_PATH):
        try:
            with open(PARAMS_PATH, 'r', encoding='utf-8') as f:
                params = json.load(f)
        except Exception:
            pass

    # Always keep default parameters
    if "default" not in params:
        params["default"] = {
            "gap_threshold_pct": 0.003,
            "rsi_overbought": 65.0,
            "rsi_oversold": 35.0,
            "failed_limit_up_vol_mult": 1.2,
            "failed_limit_up_shadow_pct": 0.02,
            "trend_window": 60
        }

    for symbol, name in holdings:
        query = "SELECT date, close as Close, high as High, low as Low, volume as Volume FROM daily_prices WHERE symbol = ? ORDER BY date ASC"
        df = pd.read_sql_query(query, conn, params=(symbol,))
        if df.empty:
            print(f"  No data found for {symbol} ({name}). Skipping.")
            continue

        df['date'] = pd.to_datetime(df['date'])
        df = df.set_index('date')

        # We need at least 80 days of history for rolling MA and outcome checks
        if len(df) < 80:
            print(f"  Insufficient data ({len(df)} bars) for {symbol}. Skipping.")
            continue

        print(f"  Optimizing {symbol} ({name})...")
        optimized = optimize_symbol(symbol, name, df)
        params[symbol] = optimized
        print(f"    Best parameters: {optimized}")

    conn.close()

    # Save parameters
    os.makedirs(os.path.dirname(PARAMS_PATH), exist_ok=True)
    with open(PARAMS_PATH, 'w', encoding='utf-8') as f:
        json.dump(params, f, indent=2, ensure_ascii=False)
    print(f"Optimization completed. Saved parameters to {PARAMS_PATH}.")

if __name__ == '__main__':
    main()
