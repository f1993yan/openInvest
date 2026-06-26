"""utils/browser_scraper.py — 用 Playwright Chromium 模拟浏览器抓取东方财富数据

akshare 的 requests 直连东方财富经常被反爬封 IP，这里用真实浏览器绕过。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

_BROWSER = None
_CONTEXT = None


def _get_page(url: str, *, timeout: int = 30000):
    """打开一个页面，返回 page 对象。复用同一个浏览器实例。"""
    global _BROWSER, _CONTEXT
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("playwright 未安装，浏览器爬虫不可用")
        return None

    if _BROWSER is None:
        pw = sync_playwright().start()
        _BROWSER = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
        )
        _CONTEXT = _BROWSER.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
        )
        _CONTEXT.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            "window.chrome={runtime:{}};"
        )

    page = _CONTEXT.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        return page
    except Exception as e:
        log.warning("playwright goto %s failed: %s", url, e)
        page.close()
        return None


def fetch_northbound_stocks_browser(max_stocks: int = 20) -> List[Dict[str, Any]]:
    """用浏览器拦截东方财富北向个股 API（备用，被限流时返回空）。"""
    try:
        import requests
        # 先试直接 API
        url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
        params = {
            "sortColumns": "ADD_MARKET_CAP", "sortTypes": -1,
            "pageSize": max_stocks, "pageNumber": 1,
            "reportName": "RPT_MUTUAL_STOCK_NORTHSTA",
            "columns": "SECURITY_CODE,SECURITY_NAME,ADD_MARKET_CAP,CHANGE_RATE,HOLD_SHARES,HOLD_MARKET_CAP",
            "source": "WEB", "client": "WEB",
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://data.eastmoney.com/hsgtcg/list.html",
        }
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        body = resp.json()
        data = (body.get("result") or {}).get("data") or []
        if data:
            results = []
            for item in data[:max_stocks]:
                symbol = str(item.get("SECURITY_CODE", "")).strip()
                name = str(item.get("SECURITY_NAME", "")).strip()
                net_buy = float(item.get("ADD_MARKET_CAP", 0) or 0)
                change_pct = float(item.get("CHANGE_RATE", 0) or 0)
                if symbol and len(symbol) == 6:
                    results.append({
                        "symbol": symbol, "name": name, "market": "A股",
                        "net_buy_cny": net_buy, "change_pct": change_pct,
                        "source": "eastmoney_datacenter",
                        "reason": f"北向净买 {net_buy/10000:,.0f}万" if abs(net_buy) > 10000 else "北向持仓",
                    })
            log.info("东方财富北向个股: %d 只", len(results))
            return results
    except Exception:
        pass

    # 降级到浏览器拦截
    page = _get_page("https://data.eastmoney.com/hsgtcg/list.html", timeout=30000)
    if page is None:
        return []

    api_data: List[Dict] = []

    def _on_response(resp):
        url = resp.url
        if "push2" in url and "clist" in url:
            try:
                body = resp.json()
                diff = body.get("data", {}).get("diff", [])
                if isinstance(diff, list):
                    api_data.extend(diff)
                elif isinstance(diff, dict):
                    api_data.extend(list(diff.values()))
            except Exception:
                pass

    page.on("response", _on_response)
    results: List[Dict[str, Any]] = []
    try:
        page.reload(wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(8000)

        for item in api_data[:max_stocks]:
            symbol = str(item.get("f12", "")).strip()
            name = str(item.get("f14", "")).strip()
            net_buy = float(item.get("f62", 0) or 0)
            change_pct = float(item.get("f3", 0) or 0)
            if symbol and len(symbol) == 6:
                results.append({
                    "symbol": symbol, "name": name, "market": "A股",
                    "net_buy_cny": net_buy, "change_pct": change_pct,
                    "source": "eastmoney_browser",
                    "reason": f"北向净买 {net_buy/10000:,.0f}万" if abs(net_buy) > 10000 else "北向持仓",
                })

        log.info("浏览器抓取北向个股: %d 只", len(results))
    except Exception as e:
        log.warning("浏览器抓取北向个股失败: %s", e)
    finally:
        page.close()

    return results


def fetch_sector_flow_browser(max_sectors: int = 10) -> List[Dict[str, Any]]:
    """直接调用东方财富板块资金流 API（带正确 headers，不经过浏览器）。"""
    try:
        import requests
    except ImportError:
        return []

    url = "https://data.eastmoney.com/dataapi/bkzj/getbkzj"
    params = {
        "key": "f62", "code": "m:90+s:4",
        "p": 1, "pz": max_sectors, "po": 1, "np": 1,
        "ut": "b2884a19c7fd4e889ee4437c423b4575",
        "fltt": 2, "invt": 2,
        "fields": "f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87",
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://data.eastmoney.com/bkzj/hy.html",
    }
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        diff = resp.json().get("data", {}).get("diff", [])
    except Exception as e:
        log.warning("东方财富板块资金流API失败: %s", e)
        return []

    results: List[Dict[str, Any]] = []
    for item in diff[:max_sectors]:
        sector = str(item.get("f14", "")).strip()
        change_pct = float(item.get("f3", 0) or 0)
        net_inflow = float(item.get("f62", 0) or 0)
        if sector:
            results.append({
                "sector": sector, "change_pct": change_pct,
                "main_net_inflow_cny": net_inflow,
                "fund_flow_source": "eastmoney_api", "leaders": [],
            })

    log.info("东方财富板块资金流: %d 个板块", len(results))
    return results


def fetch_baidu_hot_browser(max_items: int = 20) -> List[Dict[str, str]]:
    """用浏览器抓取百度热搜（备用方案，baidu_hot API 超时时用）。"""
    url = "https://top.baidu.com/board?tab=realtime"
    page = _get_page(url, timeout=20000)
    if page is None:
        return []

    results: List[Dict[str, str]] = []
    try:
        page.wait_for_selector("[class*=title_]", timeout=8000)
        items = page.query_selector_all("[class*=title_]")
        for item in items[:max_items]:
            title = item.inner_text().strip()
            if title and len(title) > 2:
                results.append({"title": title})
        log.info("浏览器抓取百度热搜: %d 条", len(results))
    except Exception as e:
        log.warning("浏览器抓取百度热搜失败: %s", e)
    finally:
        page.close()

    return results


def close_browser():
    """关闭浏览器实例（进程退出时调用）。"""
    global _BROWSER, _CONTEXT
    try:
        if _CONTEXT:
            _CONTEXT.close()
        if _BROWSER:
            _BROWSER.close()
    except Exception:
        pass
    _BROWSER = None
    _CONTEXT = None
