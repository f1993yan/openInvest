"""utils/cn_market_provider.py — 国内可直连行情源，替代 yfinance。

Yahoo 对 A股/港股从国内访问经常 429 / 超时，本模块改走国内源（全部免 auth，
实测可直连）：
- Sina  hq.sinajs.cn        : A股/指数/汇率/VIX/美股/黄金 实时
- Sina  quotes.sina.cn      : A股 + 指数 日线 OHLCV 历史
- Tencent web.ifzq.gtimg.cn : 港股 日线 OHLCV 历史
- Tencent qt.gtimg.cn       : 港股 实时

对外暴露（给 exchange_fee / gold_price 当 yfinance 的 drop-in）：
- fetch_history(symbol, period) -> pd.DataFrame  (index=日期, cols=Close[/High/Low/Volume])
- fetch_spot(symbol) -> Optional[float]
- is_supported(symbol) -> bool

symbol 用 yfinance 风格（601179.SS / 300476.SZ / 00700.HK / ^VIX / USDCNY=X /
GC=F / 000001.SS），内部映射到各源 symbol。不支持的 symbol（如 ^TNX / NDQ.AX）
返回空 DataFrame / None，由 caller 走原有 DB / CSV 兜底，不抛异常。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Optional, Tuple

import pandas as pd
import requests

log = logging.getLogger(__name__)

_TIMEOUT = 12
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
_SINA_REFERER = "https://finance.sina.com.cn"


# ============================================================
# symbol 映射
# ============================================================
def _map_symbol(symbol: str) -> Optional[dict]:
    """yfinance 风格 symbol → 内部路由信息。不支持返回 None。

    返回 dict: {kind, sina, tx, em_secid}
      kind: 'ashare' | 'hk' | 'index_cn' | 'fx' | 'vix' | 'gold' | 'us'
    """
    s = symbol.strip().upper()

    # A股 / 指数（沪 .SS / 深 .SZ）
    m = re.match(r"^(\d{6})\.(SS|SZ)$", s)
    if m:
        code, ex = m.group(1), m.group(2)
        pre = "sh" if ex == "SS" else "sz"
        # 6/000/399 开头的指数 vs 个股都用同一 realtime/kline 接口，sina 前缀一致
        is_index = (ex == "SS" and code.startswith("000")) or (
            ex == "SZ" and code.startswith("399")
        )
        return {
            "kind": "index_cn" if is_index else "ashare",
            "sina": f"{pre}{code}",
            "em_secid": f"{'1' if ex == 'SS' else '0'}.{code}",
        }

    # 港股
    m = re.match(r"^(\d{1,5})\.HK$", s)
    if m:
        code = m.group(1).zfill(5)
        return {"kind": "hk", "tx": f"hk{code}", "sina": f"rt_hk{code}"}

    if s in ("^VIX", "VIX"):
        return {"kind": "vix", "sina": "znb_VIX"}

    # 汇率 USDCNY=X / AUDCNY=X / EURCNY=X ...
    m = re.match(r"^([A-Z]{3})([A-Z]{3})=X$", s)
    if m:
        return {"kind": "fx", "sina": f"fx_s{m.group(1).lower()}{m.group(2).lower()}"}

    if s in ("GC=F", "XAUUSD=X"):
        return {"kind": "gold", "sina": "hf_GC"}

    # 美股指数（^IXIC / ^DJI）—— 备用，主流程用不到
    if s in ("^IXIC", "^DJI"):
        return {"kind": "us", "sina": "gb_ixic" if s == "^IXIC" else "gb_dji"}

    return None


def is_supported(symbol: str) -> bool:
    return _map_symbol(symbol) is not None


def _period_to_bars(period: str) -> int:
    """yfinance period → 想要的日线根数（取交易日近似，多取无害）。"""
    p = (period or "").strip().lower()
    try:
        if p.endswith("mo"):
            return max(int(p[:-2] or 1) * 23, 5)
        if p.endswith("y"):
            return max(int(p[:-1] or 1) * 252, 30)
        if p.endswith("d"):
            return max(int(p[:-1] or 1), 2)
    except ValueError:
        pass
    return 30


# ============================================================
# HTTP
# ============================================================
def _http_get(url: str, *, gbk: bool = False, referer: Optional[str] = None) -> Optional[str]:
    headers = {"User-Agent": _UA}
    if referer:
        headers["Referer"] = referer
    try:
        r = requests.get(url, headers=headers, timeout=_TIMEOUT)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        log.warning(f"[cn_market] GET 失败 {url[:80]}: {e}")
        return None
    if gbk:
        return r.content.decode("gbk", errors="ignore")
    return r.text


def _sina_fields(text: str, sina_sym: str) -> Optional[list]:
    """解析 hq.sinajs.cn 单行 var hq_str_xxx="a,b,c,..."; → 字段 list。"""
    if not text:
        return None
    m = re.search(rf'hq_str_{re.escape(sina_sym)}="([^"]*)"', text)
    if not m or not m.group(1):
        return None
    return m.group(1).split(",")


# ============================================================
# 实时价
# ============================================================
def fetch_spot(symbol: str) -> Optional[float]:
    """拉单个 symbol 的最新价。失败返回 None（caller 兜底）。"""
    info = _map_symbol(symbol)
    if info is None:
        return None
    kind = info["kind"]
    try:
        if kind == "hk":
            return _spot_hk(info)
        return _spot_sina(info, kind)
    except Exception as e:  # noqa: BLE001
        log.warning(f"[cn_market] spot {symbol} 失败: {e}")
        return None


def _spot_sina(info: dict, kind: str) -> Optional[float]:
    txt = _http_get(f"https://hq.sinajs.cn/list={info['sina']}",
                    gbk=True, referer=_SINA_REFERER)
    f = _sina_fields(txt, info["sina"])
    if not f:
        return None
    # 各源字段布局不同：
    # A股/指数:  [0]=name [1]=open [2]=prevclose [3]=price ...
    # fx_s*:     [0]=time [1]=price ...
    # znb_VIX:   [0]=name [1]=price ...
    # hf_GC:     [0]=price ...
    # gb_*(美股):[0]=name [1]=price ...
    try:
        if kind in ("ashare", "index_cn"):
            return float(f[3])
        if kind == "fx":
            return float(f[1])
        if kind == "vix":
            return float(f[1])
        if kind == "gold":
            return float(f[0])
        if kind == "us":
            return float(f[1])
    except (ValueError, IndexError):
        return None
    return None


def _spot_hk(info: dict) -> Optional[float]:
    txt = _http_get(f"https://qt.gtimg.cn/q={info['tx']}", gbk=True)
    if not txt:
        return None
    m = re.search(r'="([^"]*)"', txt)
    if not m:
        return None
    parts = m.group(1).split("~")
    try:
        return float(parts[3])      # [3]=当前价
    except (ValueError, IndexError):
        return None


# ============================================================
# 历史 OHLCV
# ============================================================
def fetch_history(symbol: str, period: str = "2y") -> pd.DataFrame:
    """拉日线 OHLCV，返回与 MarketStore.get_history_df 同构的 DataFrame
    (DatetimeIndex + Close/High/Low/Volume 列)。失败返回空 DataFrame。"""
    info = _map_symbol(symbol)
    if info is None:
        return pd.DataFrame()
    bars = _period_to_bars(period)
    try:
        if info["kind"] == "hk":
            rows = _hist_hk(info, bars)
        elif info["kind"] in ("ashare", "index_cn"):
            rows = _hist_sina_cn(info, bars)
        else:
            # fx / vix / gold / us：无免费日线源，用实时价合成单行 df，
            # 让 _safe_close / fx.get_fx_rate 至少拿到当前值（不再 0 兜底）。
            spot = fetch_spot(symbol)
            if spot is None:
                return pd.DataFrame()
            today = datetime.now().strftime("%Y-%m-%d")
            return _rows_to_df([(today, spot, spot, spot, None)])
    except Exception as e:  # noqa: BLE001
        log.warning(f"[cn_market] history {symbol} 失败: {e}")
        return pd.DataFrame()
    return _rows_to_df(rows)


def _rows_to_df(rows: list[Tuple]) -> pd.DataFrame:
    """rows: [(date_str, close, high, low, volume), ...] → DataFrame。"""
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["Date", "Close", "High", "Low", "Volume"])
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.set_index("Date").sort_index()
    for col in ("Close", "High", "Low", "Volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _hist_sina_cn(info: dict, bars: int) -> list[Tuple]:
    """Sina 日线 K：quotes.sina.cn CN_MarketDataService。scale=240=日线。"""
    import json
    datalen = min(max(bars, 5), 1023)
    url = (
        "https://quotes.sina.cn/cn/api/json_v2.php/"
        f"CN_MarketDataService.getKLineData?symbol={info['sina']}"
        f"&scale=240&ma=no&datalen={datalen}"
    )
    txt = _http_get(url, referer=_SINA_REFERER)
    if not txt:
        return []
    txt = txt.strip()
    if not txt.startswith("["):
        return []
    data = json.loads(txt)
    out = []
    for d in data:
        out.append((d["day"], d.get("close"), d.get("high"),
                    d.get("low"), d.get("volume")))
    return out


def _hist_hk(info: dict, bars: int) -> list[Tuple]:
    """Tencent 港股日线：web.ifzq.gtimg.cn hkfqkline。qfqday=前复权日线。"""
    import json
    n = min(max(bars, 5), 800)
    url = (
        "https://web.ifzq.gtimg.cn/appstock/app/hkfqkline/get?"
        f"param={info['tx']},day,,,{n},qfq"
    )
    txt = _http_get(url)
    if not txt:
        return []
    data = json.loads(txt)
    node = (data.get("data") or {}).get(info["tx"], {})
    klines = node.get("qfqday") or node.get("day") or []
    out = []
    for k in klines:
        # [date, open, close, high, low, volume, ...]
        if len(k) < 6:
            continue
        out.append((k[0], k[2], k[3], k[4], k[5]))
    return out
