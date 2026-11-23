#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from datetime import datetime, timezone, timedelta
from pathlib import Path
import requests

# ================= 配置项 =================
OUTPUT_HTML = Path(__file__).resolve().parent / "index.html"
LOOKBACK_DAYS = 250   # 250 ≈ 52周前高；如需历史前高可改为 3000
EQUITY_WEIGHT = 0.95  # QDII ETF 估算仓位权重
TIMEOUT = 10
BJT = timezone(timedelta(hours=8))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://fundf10.eastmoney.com/",
}

PAIRS = [
    {
        "group": "纳斯达克 100",
        "us_name": "QQQ",
        "us_secid": "105.QQQ",
        "cn_name": "嘉实纳指100ETF",
        "cn_code": "159501",
        "cn_secid": "0.159501",
    },
    {
        "group": "标普 500",
        "us_name": "SPY",
        "us_secid": "107.SPY",
        "cn_name": "博时标普500ETF",
        "cn_code": "513500",
        "cn_secid": "1.513500",
    },
]


def fetch_realtime_quote(secid: str) -> dict:
    url = "https://push2.eastmoney.com/api/qt/stock/get"
    params = {"secid": secid, "fields": "f43,f57,f58,f59,f60,f170"}
    resp = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT).json()
    d = resp["data"]
    scale = 10 ** int(d["f59"])
    price = d["f43"] / scale if isinstance(d["f43"], (int, float)) else d["f60"] / scale
    prev_close = d["f60"] / scale
    pct = d["f170"] / 100.0 if isinstance(d["f170"], (int, float)) else (price / prev_close - 1) * 100
    return {"price": price, "prev_close": prev_close, "pct": pct}


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


def find_close_on_or_before(klines: list, target_date: str) -> float:
    for row in reversed(klines):
        if row["date"] <= target_date:
            return row["close"]
    return klines[-1]["close"]


def compute_pair_metrics(pair: dict, fx_now: float, fx_klines: list) -> dict:
    us_klines = fetch_klines(pair["us_secid"])
    cn_klines = fetch_klines(pair["cn_secid"])
    cn_quote = fetch_realtime_quote(pair["cn_secid"])
    nav_info = fetch_latest_nav(pair["cn_code"])

    us_latest = us_klines[-1]
    us_prev_close = us_klines[-2]["close"]
    us_pct = (us_latest["close"] / us_prev_close - 1) * 100
    us_high_row = max(us_klines, key=lambda x: x["high"])
    us_drawdown = (us_latest["close"] / us_high_row["high"] - 1) * 100

    cn_price = cn_quote["price"]
    cn_high_val = max(max(r["high"] for r in cn_klines), cn_price)
    cn_high_row = max(cn_klines, key=lambda x: x["high"])
    cn_high_date = datetime.now(BJT).strftime("%Y-%m-%d") if cn_price >= cn_high_row["high"] else cn_high_row["date"]
    cn_drawdown = (cn_price / cn_high_val - 1) * 100

    nav_date = nav_info["date"]
    us_on_nav_date = find_close_on_or_before(us_klines, nav_date)
    fx_on_nav_date = find_close_on_or_before(fx_klines, nav_date)

    us_cum_ret = us_latest["close"] / us_on_nav_date - 1
    fx_ratio = fx_now / fx_on_nav_date

    iopv_est = nav_info["nav"] * (1 + us_cum_ret * EQUITY_WEIGHT) * fx_ratio
    premium_pct = (cn_price / iopv_est - 1) * 100

    k_ratio = iopv_est / us_latest["close"]
    equiv_us_price = cn_price / k_ratio
    equiv_us_drawdown = (equiv_us_price / us_high_row["high"] - 1) * 100

    return {
        "group": pair["group"],
        "us_name": pair["us_name"],
        "us_date": us_latest["date"],
        "us_close": us_latest["close"],
        "us_pct": us_pct,
        "us_high": us_high_row["high"],
        "us_high_date": us_high_row["date"],
        "us_drawdown": us_drawdown,
        "cn_name": pair["cn_name"],
        "cn_code": pair["cn_code"],
        "cn_price": cn_price,
        "cn_pct": cn_quote["pct"],
        "cn_high": cn_high_val,
        "cn_high_date": cn_high_date,
        "cn_drawdown": cn_drawdown,
        "nav": nav_info["nav"],
        "nav_date": nav_date,
        "iopv_est": iopv_est,
        "premium_pct": premium_pct,
        "k_ratio": k_ratio,
        "equiv_us_price": equiv_us_price,
        "equiv_us_drawdown": equiv_us_drawdown,
        "price_gap": equiv_us_price - us_latest["close"],
    }


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
        us_pct_cls = "up" if r["us_pct"] >= 0 else "down"
        cn_pct_cls = "up" if r["cn_pct"] >= 0 else "down"
        cards_html.append(f"""
        <section class="card">
          <div class="card-header">
            <h2>{r['group']}：{r['us_name']} vs {r['cn_name']} ({r['cn_code']})</h2>
            <span class="badge {b_cls}">实时溢价 {r['premium_pct']:+.2f}%</span>
          </div>
          <div class="grid">
            <div class="box">
              <div class="label">美股 {r['us_name']} 收盘价（{r['us_date']}）</div>
              <div class="value">${r['us_close']:.2f} <span class="{us_pct_cls}">({r['us_pct']:+.2f}%)</span></div>
              <div class="sub">前高 ${r['us_high']:.2f}（{r['us_high_date']}）</div>
              <div class="sub">距前高回撤：<strong>{r['us_drawdown']:+.2f}%</strong></div>
            </div>
            <div class="box">
              <div class="label">A股 {r['cn_code']} 实时价</div>
              <div class="value">¥{r['cn_price']:.3f} <span class="{cn_pct_cls}">({r['cn_pct']:+.2f}%)</span></div>
              <div class="sub">前高 ¥{r['cn_high']:.3f}（{r['cn_high_date']}）</div>
              <div class="sub">距前高回撤：<strong>{r['cn_drawdown']:+.2f}%</strong></div>
            </div>
            <div class="box highlight">
              <div class="label">当前买入 {r['cn_code']} 等效 {r['us_name']} 点位</div>
              <div class="value">${r['equiv_us_price']:.2f}</div>
              <div class="sub">比美股收盘贵 <strong>${r['price_gap']:+.2f}</strong>（等效距前高 {r['equiv_us_drawdown']:+.2f}%）</div>
              <div class="sub">估算净值 ¥{r['iopv_est']:.4f} ｜ 官方净值 ¥{r['nav']:.4f}（{r['nav_date']}）</div>
            </div>
          </div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{r['us_name']} 情景</th>
                  <th>{r['us_name']} 点位</th>
                  <th>0% 溢价</th>
                  <th>+3% 溢价</th>
                  <th>+5% 溢价</th>
                  <th>当前 ({r['premium_pct']:+.1f}%)</th>
                </tr>
              </thead>
              <tbody>
                {''.join(
                    f"<tr><td>{label}</td><td>${r['us_close']*mult:.2f}</td>"
                    f"<td>¥{r['k_ratio']*r['us_close']*mult:.3f}</td>"
                    f"<td>¥{r['k_ratio']*r['us_close']*mult*1.03:.3f}</td>"
                    f"<td>¥{r['k_ratio']*r['us_close']*mult*1.05:.3f}</td>"
                    f"<td><strong>¥{r['k_ratio']*r['us_close']*mult*(1+r['premium_pct']/100):.3f}</strong></td></tr>"
                    for label, mult in [("-5%", 0.95), ("-2%", 0.98), ("昨收", 1.00), ("+2%", 1.02), ("+5%", 1.05)]
                )}
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
  <title>QQQ/SPY 与 A股ETF 盘中监控</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #f4f6f9; color: #1f2937; margin: 0; padding: 16px; }}
    .container {{ max-width: 1080px; margin: 0 auto; }}
    header {{ margin-bottom: 16px; }}
    header h1 {{ margin: 0 0 6px; font-size: 20px; }}
    .meta {{ color: #6b7280; font-size: 12px; }}
    .card {{ background: #fff; border-radius: 12px; padding: 16px; margin-bottom: 16px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); }}
    .card-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; flex-wrap: wrap; gap: 8px; }}
    .card-header h2 {{ margin: 0; font-size: 16px; }}
    .badge {{ padding: 4px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; }}
    .badge-ok {{ background: #ecfdf5; color: #047857; }}
    .badge-warn {{ background: #fffbeb; color: #b45309; }}
    .badge-danger {{ background: #fef2f2; color: #b91c1c; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; margin-bottom: 14px; }}
    .box {{ background: #f9fafb; border: 1px solid #e5e7eb; border-radius: 10px; padding: 12px; }}
    .box.highlight {{ background: #eff6ff; border-color: #bfdbfe; }}
    .label {{ font-size: 12px; color: #6b7280; margin-bottom: 4px; }}
    .value {{ font-size: 22px; font-weight: 700; margin-bottom: 4px; }}
    .sub {{ font-size: 12px; color: #4b5563; line-height: 1.5; }}
    .up {{ color: #dc2626; font-size: 14px; }}
    .down {{ color: #16a34a; font-size: 14px; }}
    .table-wrap {{ overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 12px; min-width: 480px; }}
    th, td {{ padding: 8px 6px; border-bottom: 1px solid #e5e7eb; text-align: right; }}
    th:first-child, td:first-child {{ text-align: left; }}
    th {{ background: #f9fafb; color: #4b5563; font-weight: 600; }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <h1>QQQ / SPY 与 A股跨境 ETF 盘中监控</h1>
      <div class="meta">更新时间（北京时间）：{updated_at} ｜ USD/CNH：{fx_now:.4f}</div>
    </header>
    {''.join(cards_html)}
  </div>
</body>
</html>"""


def main():
    fx_quote = fetch_realtime_quote("133.USDCNH")
    fx_klines = fetch_klines("133.USDCNH", limit=30)
    results = [compute_pair_metrics(p, fx_quote["price"], fx_klines) for p in PAIRS]

    updated_at = datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")
    html = render_html(results, fx_quote["price"], updated_at)
    OUTPUT_HTML.write_text(html, encoding="utf-8")
    print(f"[{updated_at}] 已更新：{OUTPUT_HTML}")


if __name__ == "__main__":
    main()
