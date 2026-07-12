"""scripts/export_accuracy.py — verdict_review.jsonl 脱敏聚合脚本

读 memory/.dreams/verdict_review.jsonl，按时间窗口（last 30d / 90d / all）聚合
方向命中率，输出到 docs/accuracy_summary.json。

**脱敏红线**：
- 绝对不输出 symbol / threshold / verdict 原文 / asset / 任何持仓字段
- 只输出命中率聚合数字 + 样本量
- by_direction 按 bullish / bearish / hold 分组（不出现具体标的）

用法：
    python scripts/export_accuracy.py
    python scripts/export_accuracy.py --jsonl path/to/custom.jsonl
    python scripts/export_accuracy.py --out path/to/output.json

条件：
- hits 里至少有一个时间窗口为 True/False 才计入方向命中统计
- hits 全空（{}）的记录跳过（backtest 缺数据的条目）
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).parent.parent

# 默认输入 / 输出路径
DEFAULT_JSONL = ROOT / "memory" / ".dreams" / "verdict_review.jsonl"
DEFAULT_OUT = ROOT / "docs" / "accuracy_summary.json"

# 公开数据红线 #2：命中率 n < 30 不对外展示具体数字（防小样本被截图误传）。
# 与 GUI invest-gui/src/routes/PublicStats.tsx 的 MIN_SAMPLE_FOR_DISPLAY 保持同一阈值——
# 这里在「数据层」就把小样本 rate 置 null，避免任何人直接 curl 公开 JSON 拿到
# GUI 本该屏蔽的小样本数字。hit/total 计数保留（GUI 仍展示 n）。
MIN_SAMPLE_FOR_PUBLIC = 30
HIT_HORIZON = "30d"
HIT_HORIZON_DAYS = 30

# expected_direction → 方向组映射（verdict 原文不出现在输出里）
_DIRECTION_MAP: Dict[str, str] = {
    "up": "bullish",
    "bullish": "bullish",
    "down": "bearish",
    "bearish": "bearish",
    "flat": "hold",
    "hold": "hold",
    "neutral": "hold",
}


def _parse_date(date_str: str) -> Optional[datetime]:
    """将 YYYY-MM-DD 解析为 UTC aware datetime，无效时返回 None"""
    try:
        return datetime.strptime(date_str.strip(), "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )
    except (ValueError, AttributeError):
        return None


def _row_is_hit(row: Dict[str, Any], hit_horizon: str = HIT_HORIZON) -> Optional[bool]:
    """Return only the explicitly requested horizon; never mix maturities."""
    val = (row.get("hits") or {}).get(hit_horizon)
    return val if isinstance(val, bool) else None


def _wilson_lower(hit: int, total: int, z: float = 1.96) -> Optional[float]:
    if total <= 0:
        return None
    p = hit / total
    denom = 1.0 + z * z / total
    center = p + z * z / (2.0 * total)
    margin = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * total)) / total)
    return round(max(0.0, (center - margin) / denom), 4)


def _load_rows(jsonl_path: Path) -> List[Dict[str, Any]]:
    """逐行解析 jsonl，跳过格式异常行"""
    rows = []
    if not jsonl_path.exists():
        return rows
    for lineno, line in enumerate(jsonl_path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            print(f"  警告：跳过第 {lineno} 行（JSON 解析失败）", file=sys.stderr)
    return rows


def _aggregate(
    rows: List[Dict[str, Any]],
    hit_horizon: str = HIT_HORIZON,
) -> Dict[str, Any]:
    """Aggregate one fixed horizon and its same-sample market base rate."""
    # by_direction：bullish / bearish / hold 各自的 hit / total
    by_dir: Dict[str, Dict[str, int]] = {
        "bullish": {"hit": 0, "total": 0},
        "bearish": {"hit": 0, "total": 0},
        "hold": {"hit": 0, "total": 0},
    }
    total_hit = 0
    total_count = 0
    market_counts = {"up": 0, "down": 0, "flat": 0}
    market_total = 0
    actual_totals = {"up": 0, "down": 0, "flat": 0}
    actual_correct = {"up": 0, "down": 0, "flat": 0}

    for row in rows:
        # hits 全空 → 跳过
        is_hit = _row_is_hit(row, hit_horizon)
        if is_hit is None:
            continue

        market_direction = str(
            (row.get("directions") or {}).get(hit_horizon) or ""
        ).lower()
        if market_direction not in market_counts:
            continue
        market_counts[market_direction] += 1
        market_total += 1

        # 方向分类（不输出 verdict 原文，只用 expected_direction 映射）
        raw_dir = str(row.get("expected_direction") or "").lower().strip()
        bucket = _DIRECTION_MAP.get(raw_dir, "hold")
        predicted_market_direction = {
            "bullish": "up",
            "bearish": "down",
            "hold": "flat",
        }[bucket]
        actual_totals[market_direction] += 1
        if predicted_market_direction == market_direction:
            actual_correct[market_direction] += 1

        by_dir[bucket]["total"] += 1
        if is_hit:
            by_dir[bucket]["hit"] += 1

        total_count += 1
        if is_hit:
            total_hit += 1

    # 计算各方向命中率（四舍五入 2 位）
    by_dir_out: Dict[str, Any] = {}
    for bucket, counts in by_dir.items():
        t = counts["total"]
        h = counts["hit"]
        by_dir_out[bucket] = {
            "hit": h,
            "total": t,
            "rate": round(h / t, 4) if t > 0 else None,
        }

    hit_rate = round(total_hit / total_count, 4) if total_count > 0 else None
    actual_recalls = [
        actual_correct[key] / actual_totals[key]
        for key in ("up", "down", "flat")
        if actual_totals[key] > 0
    ]
    balanced_accuracy = (
        round(sum(actual_recalls) / len(actual_recalls), 4)
        if actual_recalls else None
    )
    base_rate = {
        key: (round(value / market_total, 4) if market_total > 0 else None)
        for key, value in market_counts.items()
    }
    majority_rate = max((value for value in base_rate.values() if value is not None), default=None)
    return {
        "direction_hit_rate": hit_rate,
        "direction_hit_rate_wilson_lower": _wilson_lower(total_hit, total_count),
        "balanced_accuracy": balanced_accuracy,
        "accuracy_edge_vs_majority": (
            round(hit_rate - majority_rate, 4)
            if hit_rate is not None and majority_rate is not None else None
        ),
        "sample_size": total_count,
        "hit_horizon": hit_horizon,
        "by_direction": by_dir_out,
        "base_rate": {"n": market_total, **base_rate},
    }


def _filter_by_window(
    rows: List[Dict[str, Any]],
    days: Optional[int],
    now: datetime,
) -> List[Dict[str, Any]]:
    """筛选 days 天内的记录；days=None 表示全部"""
    if days is None:
        return rows
    cutoff = now - timedelta(days=days)
    result = []
    for row in rows:
        dt = _parse_date(str(row.get("date") or ""))
        if dt is not None and dt >= cutoff:
            result.append(row)
    return result


def _suppress_small_samples(window: Dict[str, Any]) -> Dict[str, Any]:
    """公开输出前抹掉小样本命中率（红线 #2）。

    - 窗口整体 sample_size < MIN_SAMPLE_FOR_PUBLIC → direction_hit_rate 置 None
    - 每个方向 total < MIN_SAMPLE_FOR_PUBLIC → 该方向 rate 置 None

    保留 hit / total 计数（GUI 需要展示 n=XX「样本不足」），只抹具体命中率数字。
    返回新 dict（不就地改入参，便于测试对照）。
    """
    out = dict(window)
    if int(out.get("sample_size", 0) or 0) < MIN_SAMPLE_FOR_PUBLIC:
        for field in (
            "direction_hit_rate",
            "direction_hit_rate_wilson_lower",
            "balanced_accuracy",
            "accuracy_edge_vs_majority",
        ):
            out[field] = None

    by_dir = out.get("by_direction") or {}
    new_by_dir: Dict[str, Any] = {}
    suppressed = 0
    for bucket, counts in by_dir.items():
        c = dict(counts)
        if int(c.get("total", 0) or 0) < MIN_SAMPLE_FOR_PUBLIC:
            c["rate"] = None
            c["hit"] = None
            suppressed += 1
        new_by_dir[bucket] = c
    out["by_direction"] = new_by_dir
    if suppressed == 1:
        for field in (
            "direction_hit_rate",
            "direction_hit_rate_wilson_lower",
            "balanced_accuracy",
            "accuracy_edge_vs_majority",
        ):
            out[field] = None

    base_rate = dict(out.get("base_rate") or {})
    if base_rate and int(base_rate.get("n", 0) or 0) < MIN_SAMPLE_FOR_PUBLIC:
        out["base_rate"] = {
            key: (value if key == "n" else None)
            for key, value in base_rate.items()
        }
    return out


def build_summary(jsonl_path: Path) -> Dict[str, Any]:
    """核心逻辑：读 jsonl → 按窗口聚合 → 返回脱敏 dict"""
    rows = _load_rows(jsonl_path)
    now = datetime.now(timezone.utc)

    windows = {
        "30d": _filter_by_window(rows, 30 + HIT_HORIZON_DAYS, now),
        "90d": _filter_by_window(rows, 90 + HIT_HORIZON_DAYS, now),
        "all": rows,
    }

    return {
        # 生成时间戳（UTC ISO）
        "generated_at": now.isoformat(timespec="seconds"),
        "windows": {
            name: _suppress_small_samples(_aggregate(subset, HIT_HORIZON))
            for name, subset in windows.items()
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="verdict_review.jsonl 脱敏聚合 → docs/accuracy_summary.json"
    )
    parser.add_argument(
        "--jsonl",
        type=Path,
        default=DEFAULT_JSONL,
        help=f"输入 jsonl 路径（默认 {DEFAULT_JSONL}）",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"输出 JSON 路径（默认 {DEFAULT_OUT}）",
    )
    args = parser.parse_args()

    summary = build_summary(args.jsonl)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"accuracy_summary.json 已写入: {args.out}", file=sys.stderr)
    # 同时输出到 stdout 方便脚本管道消费
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
