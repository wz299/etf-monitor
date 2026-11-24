#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
import requests

# ================= 配置项 =================
OUTPUT_HTML = Path(__file__).resolve().parent / "index.html"
LOOKBACK_DAYS = 250   # 250 ≈ 52周前高
EQUITY_WEIGHT = 0.95  # 降级推算时的权益仓位权重
TIMEOUT = (5, 8)      # (连接超时5秒, 读取超时8秒)
BJT = timezone(timedelta(hours=8))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
}

QQQ_ZONES = [
    {"low": 687, "high": 694, "tag": "（中）"},
    {"low": 668, "high": 681, "tag": "（小）"},
    {"low": 642, "high": 664, "tag": "（中）长线"},
    {"low": 617, "high": 635, "tag": "（超强）长线"},
    {"low": 585, "high": 610, "tag": "（超强）长线 重要 破位 大熊市"},
]

SPY_ZONES = [
    {"low": 702, "high": 710, "tag": "（中）长线 高点回撤10%修正区域"},
    {"low": 671, "high": 694, "tag": "（超强）止跌 长线"},
    {"low": 648, "high": 667, "tag": "（强）止跌 长线 重要 破位 大熊市"},
]

PAIRS = [
    {
        "group": "纳斯达克 100",
        "us_name": "QQQ",
        "cn_name": "嘉实纳指100ETF",
        "cn_code": "159501",
        "cn_market": "sz",
        "zones": QQQ_ZONES,
    },
    {
        "group": "纳斯达克 100",
        "us_name": "QQQ",
        "cn_name": "国泰纳指100ETF",
        "cn_code": "513100",
        "cn_market": "sh",
        "zones": QQQ_ZONES,
    },
    {
        "group": "标普 500",
        "us_name": "SPY",
        "cn_name": "博时标普500ETF",
        "cn_code": "513500",
        "cn_market": "sh",
        "zones": SPY_ZONES,
    },
]


def safe_get(url: str, params: Optional[dict] = None, headers: Optional[dict] = None) -> requests.Response:
    req_headers = HEADERS.copy()
    if headers:
        req_headers.update(headers)
    last_err = None
    for _ in range(2):
        try:
            resp = requests.get(url, params=params, headers=req_headers, timeout=TIMEOUT)
            resp.raise_for_status()
            return resp
        except Exception as e:
            last_err = e
    raise last_err


# ================= 数据抓取 =================
def fetch_yahoo_klines(symbol: str, range_str: str = "1y") -> dict:
    """从 Yahoo Finance 获取美股/汇率日线及最新价"""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    resp = safe_get(url, params={"range": range_str, "interval": "1d"}).json()
    res = resp["chart"]["result"][0]
    meta = res["meta"]
    timestamps = res["timestamp"]
    quote = res["indicators"]["quote"][0]
    highs = quote["high"]
    closes = quote["close"]

    rows = []
    for ts, h, c in zip(timestamps, highs, closes):
        if c is None or h is None:
            continue
        # 美股收盘日期按美东时间(UTC-5)格式化
        dt = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=-5))).strftime("%Y-%m-%d")
        rows.append({"date": dt, "close": float(c), "high": float(h)})

    latest_price = float(meta.get("regularMarketPrice") or rows[-1]["close"])
    return {"price": latest_price, "klines": rows}


def fetch_tencent_quotes(symbols: list) -> dict:
    """从腾讯财经批量获取 A 股 ETF 实时价"""
    url = f"https://qt.gtimg.cn/q={','.join(symbols)}"
    text = safe_get(url).text
    out = {}
    for line in text.strip().split(";"):
        if "=" not in line:
            continue
        left, right = line.split("=", 1)
        sym = left.replace("v_", "").strip()
        parts = right.strip().strip('"').split("~")
        if len(parts) > 4:
            price = float(parts[3]) if float(parts[3]) > 0 else float(parts[4])
            out[sym] = {"price": price, "prev_close": float(parts[4])}
    return out


def fetch_tencent_klines(symbol: str, limit: int = LOOKBACK_DAYS) -> list:
    """从腾讯财经获取 A 股 ETF 前复权日K线"""
    url = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    resp = safe_get(url, params={"param": f"{symbol},day,,,{limit},qfq"}).json()
    node = resp["data"][symbol]
    raw = node.get("qfqday") or node.get("day") or []
    rows = []
    for item in raw:
        rows.append({"date": item[0], "close": float(item[2]), "high": float(item[3])})
    return rows


def fetch_live_iopv(fund_code: str) -> Optional[dict]:
    """天天基金 CDN 实时估值 + 最新官方净值"""
    url = f"https://fundgz.1234567.com.cn/js/{fund_code}.js"
    try:
        text = safe_get(url, headers={"Referer": "https://fund.eastmoney.com/"}).text.strip()
        m = re.search(r"jsonpgz\((\{.*\})\)", text)
        if not m:
            return None
        d = json.loads(m.group(1))
        return {
            "iopv": float(d["gsz"]),
            "gztime": d.get("gztime", ""),
            "nav": float(d["dwjz"]),
            "nav_date": d.get("jzrq", ""),
        }
    except Exception:
        return None


def fetch_nav_history(fund_code: str, limit: int = LOOKBACK_DAYS) -> Optional[dict]:
    """天天基金 CDN 历史净值趋势（用于计算估值前高及兜底最新净值）"""
    url = f"https://fund.eastmoney.com/pingzhongdata/{fund_code}.js"
    try:
        text = safe_get(url, headers={"Referer": "https://fund.eastmoney.com/"}).text
        m = re.search(r"Data_netWorthTrend\s*=\s*(\[.*?\]);", text)
        if not m:
            return None
        arr = json.loads(m.group(1))[-limit:]
        best = max(arr, key=lambda x: x["y"])
        latest = arr[-1]
        return {
            "nav_high": float(best["y"]),
            "nav_high_date": datetime.fromtimestamp(best["x"] / 1000, tz=BJT).strftime("%Y-%m-%d"),
            "latest_nav": float(latest["y"]),
            "latest_nav_date": datetime.fromtimestamp(latest["x"] / 1000, tz=BJT).strftime("%Y-%m-%d"),
        }
    except Exception:
        return None


def find_close_on_or_before(klines: list, target_date: str) -> float:
    for row in reversed(klines):
        if row["date"] <= target_date:
            return row["close"]
    return klines[-1]["close"]


# ================= 核心计算 =================
def compute_pair_metrics(pair: dict, us_cache: dict, cn_quotes: dict, fx_now: float, fx_klines: list) -> dict:
    cn_sym = f"{pair['cn_market']}{pair['cn_code']}"
    print(f"正在计算 {pair['cn_name']} ({pair['cn_code']})...", flush=True)

    if pair["us_name"] not in us_cache:
        us_cache[pair["us_name"]] = fetch_yahoo_klines(pair["us_name"], range_str="1y")
    us_klines = us_cache[pair["us_name"]]["klines"]

    cn_klines = fetch_tencent_klines(cn_sym)
    cn_price = cn_quotes[cn_sym]["price"]
    live_iopv_info = fetch_live_iopv(pair["cn_code"])
    nav_hist = fetch_nav_history(pair["cn_code"])

    # 1. 美股前一交易日收盘与前高回撤
    us_latest = us_klines[-1]
    us_high_row = max(us_klines, key=lambda x: x["high"])
    us_drawdown = (us_latest["close"] / us_high_row["high"] - 1) * 100

    # 2. A股 ETF 实时价与前高回撤
    cn_high_row = max(cn_klines, key=lambda x: x["high"])
    cn_high_val = max(cn_high_row["high"], cn_price)
    cn_high_date = datetime.now(BJT).strftime("%Y-%m-%d") if cn_price >= cn_high_row["high"] else cn_high_row["date"]
    cn_drawdown = (cn_price / cn_high_val - 1) * 100

    # 3. 官方净值与实时估值（不含溢价）
    if nav_hist and (not live_iopv_info or nav_hist["latest_nav_date"] >= live_iopv_info["nav_date"]):
        nav_val = nav_hist["latest_nav"]
        nav_date = nav_hist["latest_nav_date"]
    elif live_iopv_info:
        nav_val = live_iopv_info["nav"]
        nav_date = live_iopv_info["nav_date"]
    else:
        raise RuntimeError(f"无法获取 {pair['cn_code']} 净值数据")

    us_on_nav_date = find_close_on_or_before(us_klines, nav_date)
    fx_on_nav_date = find_close_on_or_before(fx_klines, nav_date) if fx_klines else fx_now
    fx_ratio = fx_now / fx_on_nav_date if fx_on_nav_date else 1.0
    k_base = nav_val / us_on_nav_date

    if live_iopv_info and live_iopv_info["iopv"] > 0 and live_iopv_info["nav_date"] >= nav_date:
        iopv_val = live_iopv_info["iopv"]
        iopv_source = f"实时估值 {live_iopv_info['gztime']}"
        k_fair = k_base
    else:
        us_cum_ret = us_latest["close"] / us_on_nav_date - 1
        iopv_val = nav_val * (1 + us_cum_ret * EQUITY_WEIGHT) * fx_ratio
        iopv_source = "净值公式估算"
        k_fair = k_base * fx_ratio

    # 4. 估值前高与估值回撤
    if nav_hist:
        nav_high_val = max(nav_hist["nav_high"], iopv_val)
        nav_high_date = datetime.now(BJT).strftime("%Y-%m-%d") if iopv_val >= nav_hist["nav_high"] else nav_hist["nav_high_date"]
    else:
        nav_high_val = max(nav_val, iopv_val)
        nav_high_date = nav_date
    iopv_drawdown = (iopv_val / nav_high_val - 1) * 100

    # 5. 实时溢价率 & 实时估值等效的美股点位（不含溢价）
    premium_pct = (cn_price / iopv_val - 1) * 100
    equiv_us_by_iopv = iopv_val / k_fair
    equiv_us_drawdown = (equiv_us_by_iopv / us_high_row["high"] - 1) * 100

    # 6. 重要点位区间换算
    zone_rows = []
    for z in pair["zones"]:
        dd_low = (z["low"] / us_high_row["high"] - 1) * 100
        dd_high = (z["high"] / us_high_row["high"] - 1) * 100
        est_low = z["low"] * k_fair
        est_high = z["high"] * k_fair
        mkt_low = est_low * (1 + premium_pct / 100)
        mkt_high = est_high * (1 + premium_pct / 100)
        is_active = z["low"] <= equiv_us_by_iopv <= z["high"]
        zone_rows.append({
            "range_str": f"${z['low']} - ${z['high']}",
            "tag": z["tag"],
            "dd_str": f"{dd_low:.1f}% ~ {dd_high:.1f}%",
            "est_str": f"¥{est_low:.3f} ~ ¥{est_high:.3f}",
            "mkt_str": f"¥{mkt_low:.3f} ~ ¥{mkt_high:.3f}",
            "is_active": is_active,
        })

    return {
        "group": pair["group"],
        "us_name": pair["us_name"],
        "us_date": us_latest["date"],
        "us_close": us_latest["close"],
        "us_high": us_high_row["high"],
        "us_high_date": us_high_row["date"],
        "us_drawdown": us_drawdown,
        "cn_name": pair["cn_name"],
        "cn_code": pair["cn_code"],
        "cn_price": cn_price,
        "cn_high": cn_high_val,
        "cn_high_date": cn_high_date,
        "cn_drawdown": cn_drawdown,
        "nav": nav_val,
        "nav_date": nav_date,
        "iopv_val": iopv_val,
        "iopv_source": iopv_source,
        "nav_high": nav_high_val,
        "nav_high_date": nav_high_date,
        "iopv_drawdown": iopv_drawdown,
        "premium_pct": premium_pct,
        "equiv_us_by_iopv": equiv_us_by_iopv,
        "equiv_us_drawdown": equiv_us_drawdown,
        "zone_rows": zone_rows,
    }


# ================= 网页渲染 =================
def badge_class(premium: float) -> str:
    if premium >= 10:
        return "badge-danger"
    if premium >= 5:
        return "badge-warn"
    return "badge-ok"


def render_html(results: list, fx_now: float, updated_at: str) -> str:
    cards_html = []
    for r in results:
        b_cls = badge_class(r["premium_pct"])
        rows_html = []
        for z in r["zone_rows"]:
            tr_cls = ' class="active-zone"' if z["is_active"] else ""
            active_tag = ' <span class="now-pill">当前估值所在</span>' if z["is_active"] else ""
            rows_html.append(f"""
              <tr{tr_cls}>
                <td><strong>{z['range_str']}</strong>{active_tag}<div class="zone-tag">{z['tag']}</div></td>
                <td>{z['dd_str']}</td>
                <td><strong>{z['est_str']}</strong></td>
                <td>{z['mkt_str']}</td>
              </tr>
            """)

        cards_html.append(f"""
        <section class="card">
          <div class="card-header">
            <h2>{r['group']}：{r['us_name']} vs {r['cn_name']} ({r['cn_code']})</h2>
            <span class="badge {b_cls}">实时溢价 {r['premium_pct']:+.2f}%</span>
          </div>

          <div class="grid">
            <div class="box">
              <div class="label">美股 {r['us_name']} 收盘价（{r['us_date']}）</div>
              <div class="value">${r['us_close']:.2f}</div>
              <div class="sub">前高：${r['us_high']:.2f}（{r['us_high_date']}）</div>
              <div class="sub">距前高回撤：<strong>{r['us_drawdown']:+.2f}%</strong></div>
            </div>

            <div class="box">
              <div class="label">A股 {r['cn_code']} 实时价（含溢价）</div>
              <div class="value">¥{r['cn_price']:.3f}</div>
              <div class="sub">前高：¥{r['cn_high']:.3f}（{r['cn_high_date']}）</div>
              <div class="sub">距前高回撤：<strong>{r['cn_drawdown']:+.2f}%</strong></div>
            </div>

            <div class="box highlight">
              <div class="label">A股 {r['cn_code']} 实时估值（不含溢价）</div>
              <div class="value">¥{r['iopv_val']:.4f}</div>
              <div class="sub">等效 {r['us_name']} 点位：<strong>${r['equiv_us_by_iopv']:.2f}</strong>（距前高 {r['equiv_us_drawdown']:+.2f}%）</div>
              <div class="sub">估值前高：¥{r['nav_high']:.4f}（{r['nav_high_date']}）｜ 回撤：<strong>{r['iopv_drawdown']:+.2f}%</strong></div>
              <div class="sub muted">官方净值 ¥{r['nav']:.4f}（{r['nav_date']}）｜ {r['iopv_source']}</div>
            </div>
          </div>

          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{r['us_name']} 重要点位区间</th>
                  <th>对应距前高回撤</th>
                  <th>{r['cn_code']} 实时估值区间（不含溢价）</th>
                  <th>按当前溢价 ({r['premium_pct']:+.1f}%) 对应盘面价</th>
                </tr>
              </thead>
              <tbody>
                {''.join(rows_html)}
              </tbody>
            </table>
          </div>
        </section>
        """)

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <meta http-equiv="refresh" content="300" />
  <title>QQQ / SPY 与 A股跨境 ETF 盘中监控</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f4f6f9; color: #1f2937; margin: 0; padding: 16px; }}
    .container {{ max-width: 1080px; margin: 0 auto; }}
    header {{ margin-bottom: 16px; }}
    header h1 {{ margin: 0 0 6px; font-size: 20px; }}
    .meta {{ color: #6b7280; font-size: 12px; }}
    .card {{ background: #fff; border-radius: 12px; padding: 16px; margin-bottom: 18px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); }}
    .card-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; flex-wrap: wrap; gap: 8px; }}
    .card-header h2 {{ margin: 0; font-size: 16px; }}
    .badge {{ padding: 4px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; }}
    .badge-ok {{ background: #ecfdf5; color: #047857; }}
    .badge-warn {{ background: #fffbeb; color: #b45309; }}
    .badge-danger {{ background: #fef2f2; color: #b91c1c; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 12px; margin-bottom: 14px; }}
    .box {{ background: #f9fafb; border: 1px solid #e5e7eb; border-radius: 10px; padding: 12px; }}
    .box.highlight {{ background: #eff6ff; border-color: #bfdbfe; }}
    .label {{ font-size: 12px; color: #6b7280; margin-bottom: 4px; }}
    .value {{ font-size: 22px; font-weight: 700; margin-bottom: 6px; }}
    .sub {{ font-size: 12px; color: #374151; line-height: 1.6; }}
    .muted {{ color: #6b7280; font-size: 11px; margin-top: 2px; }}
    .table-wrap {{ overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 12px; min-width: 540px; }}
    th, td {{ padding: 10px 8px; border-bottom: 1px solid #e5e7eb; text-align: right; vertical-align: middle; }}
    th:first-child, td:first-child {{ text-align: left; }}
    th {{ background: #f9fafb; color: #4b5563; font-weight: 600; }}
    tr.active-zone {{ background: #eff6ff; }}
    .zone-tag {{ font-size: 11px; color: #6b7280; margin-top: 2px; }}
    .now-pill {{ display: inline-block; background: #2563eb; color: #fff; font-size: 10px; padding: 1px 6px; border-radius: 999px; margin-left: 6px; vertical-align: middle; }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <h1>QQQ / SPY 与 A股跨境 ETF 盘中监控</h1>
      <div class="meta">更新时间（北京时间）：{updated_at} ｜ USD/CNY：{fx_now:.4f}</div>
    </header>
    {''.join(cards_html)}
  </div>
</body>
</html>"""


def main():
    print("正在拉取汇率 (USD/CNY)...", flush=True)
    fx_data = fetch_yahoo_klines("CNY=X", range_str="1mo")
    fx_now = fx_data["price"]
    fx_klines = fx_data["klines"]

    print("正在批量拉取 A股 ETF 实时行情...", flush=True)
    cn_symbols = [f"{p['cn_market']}{p['cn_code']}" for p in PAIRS]
    cn_quotes = fetch_tencent_quotes(cn_symbols)

    us_cache = {}
    results = [compute_pair_metrics(p, us_cache, cn_quotes, fx_now, fx_klines) for p in PAIRS]

    updated_at = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")
    html = render_html(results, fx_now, updated_at)
    OUTPUT_HTML.write_text(html, encoding="utf-8")
    print(f"[{updated_at}] 更新完成：{OUTPUT_HTML}", flush=True)


if __name__ == "__main__":
    main()
