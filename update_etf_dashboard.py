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
LOOKBACK_DAYS = 250   # 250 ≈ 52周前高；如需更长周期可调大
EQUITY_WEIGHT = 0.95  # 降级推算时的权益仓位权重
TIMEOUT = 10
BJT = timezone(timedelta(hours=8))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://fundf10.eastmoney.com/",
}

# 重要点位配置（可随时按你的技术面判断调整）
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
        "us_secid": "105.QQQ",
        "cn_name": "嘉实纳指100ETF",
        "cn_code": "159501",
        "cn_secid": "0.159501",
        "zones": QQQ_ZONES,
    },
    {
        "group": "纳斯达克 100",
        "us_name": "QQQ",
        "us_secid": "105.QQQ",
        "cn_name": "国泰纳指100ETF",
        "cn_code": "513100",
        "cn_secid": "1.513100",
        "zones": QQQ_ZONES,
    },
    {
        "group": "标普 500",
        "us_name": "SPY",
        "us_secid": "107.SPY",
        "cn_name": "博时标普500ETF",
        "cn_code": "513500",
        "cn_secid": "1.513500",
        "zones": SPY_ZONES,
    },
]


# ================= 数据抓取 =================
def fetch_realtime_quote(secid: str) -> dict:
    url = "https://push2.eastmoney.com/api/qt/stock/get"
    params = {"secid": secid, "fields": "f43,f57,f58,f59,f60"}
    resp = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT).json()
    d = resp["data"]
    scale = 10 ** int(d["f59"])
    price = d["f43"] / scale if isinstance(d["f43"], (int, float)) else d["f60"] / scale
    prev_close = d["f60"] / scale
    return {"price": price, "prev_close": prev_close}


def fetch_klines(secid: str, limit: int = LOOKBACK_DAYS) -> list:
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56",
        "klt": "101",
        "fqt": "1",
        "end": "20500101",
        "lmt": str(limit),
    }
    resp = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT).json()
    rows = []
    for line in resp["data"]["klines"]:
        parts = line.split(",")
        rows.append({"date": parts[0], "close": float(parts[2]), "high": float(parts[3])})
    return rows


def fetch_latest_nav(fund_code: str) -> dict:
    url = "https://api.fund.eastmoney.com/f10/lsjz"
    params = {"fundCode": fund_code, "pageIndex": 1, "pageSize": 5}
    resp = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT).json()
    latest = resp["Data"]["LSJZList"][0]
    return {"date": latest["FSRQ"], "nav": float(latest["DWJZ"])}


def fetch_nav_high(fund_code: str, limit: int = LOOKBACK_DAYS) -> Optional[dict]:
    """获取近 LOOKBACK_DAYS 个交易日的单位净值前高"""
    url = f"https://fund.eastmoney.com/pingzhongdata/{fund_code}.js"
    try:
        text = requests.get(url, headers=HEADERS, timeout=TIMEOUT).text
        m = re.search(r"Data_netWorthTrend\s*=\s*(\[.*?\]);", text)
        if not m:
            return None
        arr = json.loads(m.group(1))[-limit:]
        best = max(arr, key=lambda x: x["y"])
        dt = datetime.fromtimestamp(best["x"] / 1000, tz=BJT).strftime("%Y-%m-%d")
        return {"nav_high": float(best["y"]), "nav_high_date": dt}
    except Exception:
        return None


def fetch_live_iopv(fund_code: str) -> Optional[dict]:
    """优先直连天天基金盘中实时估值接口，与同花顺/东财App保持一致"""
    url = f"https://fundgz.1234567.com.cn/js/{fund_code}.js"
    try:
        text = requests.get(url, headers=HEADERS, timeout=TIMEOUT).text.strip()
        m = re.search(r"jsonpgz\((\{.*\})\)", text)
        if not m:
            return None
        d = json.loads(m.group(1))
        gsz = float(d["gsz"])
        if gsz <= 0:
            return None
        return {
            "iopv": gsz,
            "gztime": d.get("gztime", ""),
            "jzrq": d.get("jzrq", ""),
        }
    except Exception:
        return None


def find_close_on_or_before(klines: list, target_date: str) -> float:
    for row in reversed(klines):
        if row["date"] <= target_date:
            return row["close"]
    return klines[-1]["close"]


# ================= 核心计算 =================
def compute_pair_metrics(pair: dict, us_cache: dict, fx_now: float, fx_klines: list) -> dict:
    if pair["us_secid"] not in us_cache:
        us_cache[pair["us_secid"]] = fetch_klines(pair["us_secid"])
    us_klines = us_cache[pair["us_secid"]]

    cn_klines = fetch_klines(pair["cn_secid"])
    cn_quote = fetch_realtime_quote(pair["cn_secid"])
    nav_info = fetch_latest_nav(pair["cn_code"])
    nav_high_info = fetch_nav_high(pair["cn_code"])
    live_iopv_info = fetch_live_iopv(pair["cn_code"])

    # 1. 美股前一交易日收盘与前高回撤
    us_latest = us_klines[-1]
    us_high_row = max(us_klines, key=lambda x: x["high"])
    us_drawdown = (us_latest["close"] / us_high_row["high"] - 1) * 100

    # 2. A股 ETF 实时价与前高回撤
    cn_price = cn_quote["price"]
    cn_high_row = max(cn_klines, key=lambda x: x["high"])
    cn_high_val = max(cn_high_row["high"], cn_price)
    cn_high_date = datetime.now(BJT).strftime("%Y-%m-%d") if cn_price >= cn_high_row["high"] else cn_high_row["date"]
    cn_drawdown = (cn_price / cn_high_val - 1) * 100

    # 3. 实时估值（不含溢价）：优先用盘中实时估值，否则降级公式推算
    nav_date = nav_info["date"]
    us_on_nav_date = find_close_on_or_before(us_klines, nav_date)
    fx_on_nav_date = find_close_on_or_before(fx_klines, nav_date)
    fx_ratio = fx_now / fx_on_nav_date

    # 锚定官方净值日期的零溢价换算系数 K（元/美元）
    k_base = nav_info["nav"] / us_on_nav_date

    if live_iopv_info and live_iopv_info["jzrq"] >= nav_date:
        iopv_val = live_iopv_info["iopv"]
        iopv_source = f"实时估值 {live_iopv_info['gztime']}"
        # 若使用实时估值，直接以 k_base 折算等效美股点位
        k_fair = k_base
    else:
        us_cum_ret = us_latest["close"] / us_on_nav_date - 1
        iopv_val = nav_info["nav"] * (1 + us_cum_ret * EQUITY_WEIGHT) * fx_ratio
        iopv_source = "净值公式估算"
        k_fair = k_base * fx_ratio

    # 4. 估值前高与估值回撤
    if nav_high_info:
        nav_high_val = max(nav_high_info["nav_high"], iopv_val)
        nav_high_date = datetime.now(BJT).strftime("%Y-%m-%d") if iopv_val >= nav_high_info["nav_high"] else nav_high_info["nav_high_date"]
    else:
        nav_high_val = max(nav_info["nav"], iopv_val)
        nav_high_date = nav_date
    iopv_drawdown = (iopv_val / nav_high_val - 1) * 100

    # 5. 实时溢价率 & 实时估值等效的美股点位（不含溢价）
    premium_pct = (cn_price / iopv_val - 1) * 100
    equiv_us_by_iopv = iopv_val / k_fair
    equiv_us_drawdown = (equiv_us_by_iopv / us_high_row["high"] - 1) * 100

    # 6. 计算重要点位对应的各口径区间
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
        "nav": nav_info["nav"],
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
      <div class="meta">更新时间（北京时间）：{updated_at} ｜ USD/CNH：{fx_now:.4f} ｜ 交易日 10:00 及 10:30-15:10 每 10 分钟刷新</div>
    </header>
    {''.join(cards_html)}
  </div>
</body>
</html>"""


def main():
    fx_quote = fetch_realtime_quote("133.USDCNH")
    fx_klines = fetch_klines("133.USDCNH", limit=30)
    us_cache = {}
    results = [compute_pair_metrics(p, us_cache, fx_quote["price"], fx_klines) for p in PAIRS]

    updated_at = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")
    html = render_html(results, fx_quote["price"], updated_at)
    OUTPUT_HTML.write_text(html, encoding="utf-8")
    print(f"[{updated_at}] 已更新：{OUTPUT_HTML}")


if __name__ == "__main__":
    main()
