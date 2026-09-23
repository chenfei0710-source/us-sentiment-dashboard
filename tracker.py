#!/usr/bin/env python3
"""
美股情绪仪表盘 - 每日自动更新
运行时间：美东 16:30（北京时间次日 04:30）
"""

import json
import base64
import requests
import yfinance as yf
from datetime import datetime, date
import pytz
import os
import sys

# CI 环境（GitHub Actions）用相对路径，本地用绝对路径
_IS_CI      = os.environ.get("CI") == "true"
_BASE       = os.path.dirname(os.path.abspath(__file__))
_LOCAL      = "/Users/admin"
OUTPUT_HTML = os.path.join(_BASE, "index.html")                if _IS_CI else f"{_LOCAL}/us_sentiment_dashboard_v2.html"
DATA_LOG    = os.path.join(_BASE, "us_sentiment_history.json") if _IS_CI else f"{_LOCAL}/us_sentiment_history.json"
STATUS_FILE = os.path.join(_BASE, "status.json")               if _IS_CI else f"{_LOCAL}/us_sentiment_status.json"


def _read_local_gh_token():
    """Read GitHub token from macOS keychain (local runs only)"""
    try:
        import subprocess
        result = subprocess.run(
            ["security", "find-internet-password", "-s", "github.com", "-w"],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        return None

# ─────────────────────────────────────────────
# 1. 数据抓取
# ─────────────────────────────────────────────

FALLBACKS = {
    "fg":   {"score": 29,    "rating": "Fear", "prev": 31},
    "vix":  {"close": 15.84, "prev": 17.84, "high52": 35.30, "low52": 13.38},
    "spx":  {"close": 7600,  "prev": 7580, "chg_pct": 0.0},
    "aaii": {"bullish": 38.0, "neutral": 22.7, "bearish": 39.3, "week": "—"},
    "poly": {"up": 50, "down": 50, "found": False},
}

def _retry(fn, label, retries=3, delay=8):
    """重试包装：最多重试 retries 次，每次间隔 delay 秒"""
    import time
    for attempt in range(1, retries + 1):
        try:
            result = fn()
            print(f"  [{label}] ✅ 第{attempt}次成功")
            return result, True
        except Exception as e:
            print(f"  [{label}] ⚠️ 第{attempt}次失败: {e}")
            if attempt < retries:
                time.sleep(delay)
    print(f"  [{label}] ❌ {retries}次均失败，使用 fallback")
    return None, False

def fetch_fear_greed():
    """CNN Fear & Greed Index"""
    def _fetch():
        url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": "https://edition.cnn.com/markets/fear-and-greed",
            "Origin": "https://edition.cnn.com",
        }
        r = requests.get(url, timeout=15, headers=headers)
        data = r.json()
        return {
            "score":  round(data["fear_and_greed"]["score"]),
            "rating": data["fear_and_greed"]["rating"],
            "prev":   round(data["fear_and_greed"]["previous_close"]),
        }
    result, ok = _retry(_fetch, "F&G")
    return result if ok else FALLBACKS["fg"]

def fetch_vix():
    """VIX 收盘价 via yfinance"""
    def _fetch():
        ticker = yf.Ticker("^VIX")
        hist = ticker.history(period="5d")
        if hist.empty:
            raise ValueError("empty history")
        close  = round(hist["Close"].iloc[-1], 2)
        prev   = round(hist["Close"].iloc[-2], 2) if len(hist) > 1 else close
        info   = ticker.info
        return {
            "close":  close, "prev": prev,
            "high52": round(info.get("fiftyTwoWeekHigh", 35.30), 2),
            "low52":  round(info.get("fiftyTwoWeekLow",  13.38), 2),
        }
    result, ok = _retry(_fetch, "VIX")
    return result if ok else FALLBACKS["vix"]

def fetch_spx():
    """SPX 收盘价 via yfinance，nan 时回退到 Google Finance 网页"""
    def _fetch():
        import math, re
        # 先尝试 yfinance
        close = prev = None
        try:
            ticker = yf.Ticker("^GSPC")
            hist = ticker.history(period="5d")
            if not hist.empty:
                c = hist["Close"].iloc[-1]
                if not math.isnan(c):
                    close = round(c, 2)
                if len(hist) > 1:
                    p = hist["Close"].iloc[-2]
                    if not math.isnan(p):
                        prev = round(p, 2)
        except Exception:
            pass

        # yfinance 失败时回退到 Google Finance 网页
        if close is None:
            r = requests.get(
                "https://www.google.com/finance/quote/.INX:INDEXSP",
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"},
                timeout=10,
            )
            # Google Finance 页面里的价格数据嵌在 JS 数据结构中
            # 找所有 7xxx.xx 格式的数字（SPX 在 7000-8000 范围）
            nums = re.findall(r'(?<![\d.])([7]\d{3}\.\d{2})(?![\d])', r.text)
            if not nums:
                # 也试 6xxx 或 8xxx 范围（以防 SPX 大幅波动）
                nums = re.findall(r'(?<![\d.])([6-9]\d{3}\.\d{2})(?![\d])', r.text)
            if not nums:
                raise ValueError("SPX: yfinance NaN and Google Finance fallback failed")
            # 取出现频率最高的值（通常是收盘价，在页面中重复出现）
            from collections import Counter
            close = round(float(Counter(nums).most_common(1)[0][0]), 2)

        if prev is None:
            prev = close
        chg_pct = round((close - prev) / prev * 100, 2)
        return {"close": close, "prev": prev, "chg_pct": chg_pct}
    result, ok = _retry(_fetch, "SPX")
    return result if ok else FALLBACKS["spx"]

def fetch_polymarket_spx_today():
    """Polymarket 当日 SPX 涨跌赔率（Gamma API events slug）"""
    def _fetch():
        today = date.today()
        slug = f"spx-up-or-down-on-{today.strftime('%B').lower()}-{today.day}-{today.year}"
        url = f"https://gamma-api.polymarket.com/events/slug/{slug}"
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            raise ValueError(f"Polymarket event not found (slug={slug}, status={r.status_code})")
        data = r.json()
        markets = data.get("markets", [])
        if not markets:
            raise ValueError(f"Polymarket event has no markets (slug={slug})")
        m = markets[0]
        outcomes      = json.loads(m.get("outcomePrices", "[]"))
        outcome_names = json.loads(m.get("outcomes", "[]"))
        if outcome_names and outcomes:
            up_idx = next((i for i, n in enumerate(outcome_names) if n.lower() == "up"), 0)
            up_pct = round(float(outcomes[up_idx]) * 100)
            return {"up": up_pct, "down": 100 - up_pct, "found": True}
        raise ValueError("Polymarket outcome prices not found")
    result, ok = _retry(_fetch, "Polymarket")
    return result if ok else FALLBACKS["poly"]

def fetch_aaii():
    """AAII 散户情绪调查；每周四更新。
    直接从 AAII 官网 sentimentsurvey 页面抓取，使用完整浏览器 headers 绕过反爬。
    """
    def _fetch():
        import re
        from bs4 import BeautifulSoup
        from datetime import datetime

        url = "https://www.aaii.com/sentimentsurvey"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Referer": "https://www.aaii.com/",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
        r = requests.get(url, timeout=15, headers=headers)
        if r.status_code != 200:
            raise ValueError(f"AAII page returned status {r.status_code}")
        soup = BeautifulSoup(r.text, 'html.parser')
        text = soup.get_text()

        # Extract week ending date (e.g. "Week ending September 16, 2026")
        date_match = re.search(r'[Ww]eek ending\s+([A-Z][a-z]+)\s+(\d{1,2}),?\s+(\d{4})', text)
        if not date_match:
            raise ValueError("AAII week ending date not found")
        dt = datetime.strptime(date_match.group(1), '%B')
        week_str = f"{dt.month}/{int(date_match.group(2))}/{int(date_match.group(3))}"

        # Extract Bullish/Neutral/Bearish percentages
        # Pattern: "Bullish 28.8% Avg 37.5% Neutral 17.9% Avg 31.0% Bearish 53.3% Avg 31.5%"
        pct_match = re.search(
            r'Bullish\s+(\d+\.?\d*)%\s*Avg\s+\d+\.?\d*%\s*Neutral\s+(\d+\.?\d*)%\s*Avg\s+\d+\.?\d*%\s*Bearish\s+(\d+\.?\d*)%',
            text
        )
        if not pct_match:
            raise ValueError("AAII sentiment percentages not found")
        bull = round(float(pct_match.group(1)), 1)
        neut = round(float(pct_match.group(2)), 1)
        bear = round(float(pct_match.group(3)), 1)
        return {"bullish": bull, "neutral": neut, "bearish": bear, "week": week_str}
    result, ok = _retry(_fetch, "AAII")
    return result if ok else FALLBACKS["aaii"]

# ─────────────────────────────────────────────
# 2. 数据持久化
# ─────────────────────────────────────────────

def load_history():
    if os.path.exists(DATA_LOG):
        with open(DATA_LOG) as f:
            return json.load(f)
    return []

def save_history(history, today_data):
    date_str = date.today().isoformat()
    # 更新或添加今日记录
    history = [h for h in history if h["date"] != date_str]
    history.append({"date": date_str, **today_data})
    history = sorted(history, key=lambda x: x["date"])[-60:]  # 保留最近60个交易日
    with open(DATA_LOG, "w") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)
    return history

# ─────────────────────────────────────────────
# 3. 生成 HTML
# ─────────────────────────────────────────────

def rating_color(score):
    if score <= 25:   return "#ff2222", "极度恐惧", "extreme-fear"
    elif score <= 44: return "#ef4444", "恐惧",     "fear"
    elif score <= 55: return "#f59e0b", "中性",     "neutral-label"
    elif score <= 75: return "#10b981", "贪婪",     "greed"
    else:             return "#00ff88", "极度贪婪", "extreme-greed"

def vix_level(v):
    if v < 15:   return "#10b981", "低恐慌"
    elif v < 25: return "#f59e0b", "正常波动"
    elif v < 35: return "#f97316", "高波动"
    else:        return "#ef4444", "恐慌"

def gen_history_sparkline(history, key):
    """生成迷你折线图数据（SVG points）"""
    vals = [h.get(key, 0) for h in history[-20:]]
    if not vals or len(vals) < 2:
        return ""
    mn, mx = min(vals), max(vals)
    rng = mx - mn if mx != mn else 1
    w, h = 120, 30
    pts = []
    for i, v in enumerate(vals):
        x = round(i / (len(vals) - 1) * w, 1)
        y = round(h - (v - mn) / rng * h, 1)
        pts.append(f"{x},{y}")
    return " ".join(pts)

def build_html(fg, vix, spx, aaii, poly, history, now_str):
    fg_color, fg_label, fg_cls = rating_color(fg["score"])
    vix_color, vix_level_label = vix_level(vix["close"])
    vix_chg = round((vix["close"] - vix["prev"]) / vix["prev"] * 100, 1)
    spx_color = "#10b981" if spx["chg_pct"] >= 0 else "#ef4444"
    spx_sign  = "+" if spx["chg_pct"] >= 0 else ""
    poly_color = "#10b981" if poly["up"] >= 60 else ("#ef4444" if poly["up"] <= 40 else "#f59e0b")

    # needle angle: 0=extreme fear(left), 100=extreme greed(right)
    # SVG arc spans 180deg; score=29 → angle=29/100*180=52.2 from left
    # needle from center (130,130), pointing at score position
    import math
    angle_deg = 180 - (fg["score"] / 100 * 180)  # 0=right, 180=left
    rad = math.radians(angle_deg)
    nx = round(130 + 90 * math.cos(rad), 1)
    ny = round(130 - 90 * math.sin(rad), 1)

    # VIX range marker position
    vix_marker_pct = round((vix["close"] - vix["low52"]) / (vix["high52"] - vix["low52"]) * 100, 1)
    vix_marker_pct = max(2, min(98, vix_marker_pct))

    # History sparklines
    fg_pts  = gen_history_sparkline(history, "fg_score")
    vix_pts = gen_history_sparkline(history, "vix_close")
    spx_pts = gen_history_sparkline(history, "spx_close")

    spk_fg  = f'<svg viewBox="0 0 120 30" width="80" height="20" style="opacity:0.6"><polyline points="{fg_pts}" fill="none" stroke="{fg_color}" stroke-width="1.5"/></svg>' if fg_pts else ""
    spk_vix = f'<svg viewBox="0 0 120 30" width="80" height="20" style="opacity:0.6"><polyline points="{vix_pts}" fill="none" stroke="{vix_color}" stroke-width="1.5"/></svg>' if vix_pts else ""
    spk_spx = f'<svg viewBox="0 0 120 30" width="80" height="20" style="opacity:0.6"><polyline points="{spx_pts}" fill="none" stroke="{spx_color}" stroke-width="1.5"/></svg>' if spx_pts else ""

    # Overall signal
    bull_signals = 0
    bear_signals = 0
    if fg["score"] < 35: bear_signals += 1   # contrarian → actually bullish, but label as fear
    if fg["score"] > 65: bull_signals += 1
    if aaii["bearish"] > 37: bear_signals += 1  # over-bearish = contrarian bull
    if vix["close"] < 20: bull_signals += 1
    if poly["up"] > 60: bull_signals += 1
    overall = "🟢 偏多" if bull_signals >= 3 else ("🔴 偏空" if bear_signals >= 3 else "🟡 中性")
    overall_color = "#10b981" if "多" in overall else ("#ef4444" if "空" in overall else "#f59e0b")

    today_date = date.today().strftime("%Y年%-m月%-d日")
    weekdays = ["周一","周二","周三","周四","周五","周六","周日"]
    weekday  = weekdays[date.today().weekday()]

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>美股情绪仪表盘 · {today_date}</title>
<style>
  :root{{
    --bg:#0a0e1a;--card:#111827;--card2:#1a2235;--border:#1f2d45;
    --text:#e2e8f0;--muted:#64748b;
    --green:#10b981;--red:#ef4444;--yellow:#f59e0b;
    --blue:#3b82f6;--purple:#8b5cf6;--orange:#f97316;
  }}
  *{{box-sizing:border-box;margin:0;padding:0;}}
  body{{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;min-height:100vh;padding:24px;}}
  .header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px;padding-bottom:16px;border-bottom:1px solid var(--border);}}
  .header h1{{font-size:1.4rem;font-weight:700;}} .header h1 span{{color:var(--blue);}}
  .date{{color:var(--muted);font-size:0.82rem;text-align:right;}} .date strong{{color:var(--text);display:block;font-size:0.95rem;}}
  .grid{{display:grid;grid-template-columns:repeat(12,1fr);gap:14px;}}
  .col-3{{grid-column:span 3;}} .col-4{{grid-column:span 4;}} .col-6{{grid-column:span 6;}} .col-8{{grid-column:span 8;}} .col-12{{grid-column:span 12;}}
  @media(max-width:900px){{.col-3,.col-4,.col-6,.col-8{{grid-column:span 12;}}}}
  .card{{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:18px;position:relative;overflow:hidden;animation:fadeUp 0.4s ease both;}}
  .card::before{{content:'';position:absolute;top:0;left:0;right:0;height:3px;border-radius:14px 14px 0 0;}}
  .card.red::before{{background:var(--red);}} .card.green::before{{background:var(--green);}}
  .card.yellow::before{{background:var(--yellow);}} .card.blue::before{{background:var(--blue);}}
  .card.purple::before{{background:var(--purple);}} .card.orange::before{{background:var(--orange);}}
  .card.multi::before{{background:linear-gradient(90deg,var(--green),var(--blue),var(--purple));}}
  @keyframes fadeUp{{from{{opacity:0;transform:translateY(10px)}}to{{opacity:1;transform:translateY(0)}}}}
  .card-label{{font-size:0.68rem;text-transform:uppercase;letter-spacing:1px;color:var(--muted);margin-bottom:8px;}}
  .card-title{{font-size:0.9rem;font-weight:600;margin-bottom:14px;}}
  .big{{font-size:2.8rem;font-weight:800;line-height:1;margin:6px 0;}}
  .badge{{display:inline-block;padding:3px 12px;border-radius:20px;font-size:0.85rem;font-weight:600;margin:4px 0;}}
  .sub{{font-size:0.75rem;color:var(--muted);margin-top:5px;}}
  .gauge-svg{{width:100%;max-width:240px;display:block;margin:0 auto;}}
  .bar-row{{margin-bottom:12px;}}
  .bar-head{{display:flex;justify-content:space-between;font-size:0.8rem;margin-bottom:4px;}}
  .bar-track{{background:var(--border);border-radius:6px;height:7px;position:relative;overflow:hidden;}}
  .bar-fill{{height:100%;border-radius:6px;transition:width 1.2s cubic-bezier(.4,0,.2,1);}}
  .avg-line{{position:absolute;top:-2px;bottom:-2px;width:2px;background:rgba(255,255,255,0.3);border-radius:2px;}}
  .range-wrap{{margin:12px 0 6px;}}
  .range-labels{{display:flex;justify-content:space-between;font-size:0.7rem;color:var(--muted);margin-bottom:3px;}}
  .range-track{{background:linear-gradient(90deg,var(--green),var(--yellow),var(--red));border-radius:6px;height:5px;position:relative;}}
  .range-dot{{position:absolute;top:-5px;width:14px;height:14px;border-radius:50%;border:2px solid var(--bg);transform:translateX(-50%);}}
  .poly-item{{display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid var(--border);}}
  .poly-item:last-child{{border-bottom:none;}}
  .poly-q{{font-size:0.82rem;max-width:60%;}}
  .poly-v{{font-size:1.1rem;font-weight:800;}}
  .tag{{display:inline-flex;align-items:center;gap:3px;font-size:0.65rem;padding:2px 7px;border-radius:20px;font-weight:600;margin-left:5px;}}
  .tag-bull{{background:rgba(16,185,129,.15);color:var(--green);border:1px solid rgba(16,185,129,.3);}}
  .tag-bear{{background:rgba(239,68,68,.15);color:var(--red);border:1px solid rgba(239,68,68,.3);}}
  .tag-warn{{background:rgba(249,115,22,.15);color:var(--orange);border:1px solid rgba(249,115,22,.3);}}
  .tl-item{{display:flex;gap:8px;margin-bottom:8px;font-size:0.78rem;}}
  .tl-dot{{width:7px;height:7px;border-radius:50%;margin-top:4px;flex-shrink:0;}}
  .insight{{background:var(--card2);border:1px solid var(--border);border-radius:8px;padding:12px;font-size:0.78rem;color:var(--muted);line-height:1.6;margin-top:12px;}}
  .live-dot{{display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--green);margin-right:4px;animation:pulse 2s infinite;}}
  @keyframes pulse{{0%,100%{{opacity:1;transform:scale(1)}}50%{{opacity:.5;transform:scale(.8)}}}}
  .spk{{vertical-align:middle;margin-left:6px;}}
  .footer{{margin-top:20px;padding-top:12px;border-top:1px solid var(--border);display:flex;justify-content:space-between;font-size:0.7rem;color:var(--muted);flex-wrap:wrap;gap:6px;}}
  .overall-box{{background:var(--card2);border:2px solid {overall_color}33;border-radius:12px;padding:14px 18px;display:flex;align-items:center;gap:14px;}}
  .overall-big{{font-size:1.6rem;font-weight:800;color:{overall_color};}}
</style>
</head>
<body>

<div class="header">
  <div>
    <h1>美股 <span>情绪仪表盘</span></h1>
    <div style="font-size:0.72rem;color:var(--muted);margin-top:2px;">
      <span class="live-dot"></span>CNN · AAII · CBOE · Polymarket · 每交易日自动更新
    </div>
  </div>
  <div class="date">
    <strong>{today_date} {weekday}</strong>
    更新于 {now_str} ET
  </div>
</div>

<!-- Overall -->
<div class="overall-box" style="margin-bottom:18px;">
  <div class="overall-big">{overall}</div>
  <div style="font-size:0.82rem;color:var(--muted);line-height:1.7;">
    <strong style="color:var(--text);">综合情绪信号</strong>｜
    F&G <strong style="color:{fg_color};">{fg["score"]} {fg_label}</strong> ·
    VIX <strong style="color:{vix_color};">{vix["close"]}</strong> ·
    今日看涨 <strong style="color:{poly_color};">{poly["up"]}%</strong> ·
    AAII 看跌 <strong style="color:var(--red);">{aaii["bearish"]}%</strong>
  </div>
</div>

<div class="grid">

<!-- Fear & Greed -->
<div class="card red col-4">
  <div class="card-label">CNN · Fear &amp; Greed Index</div>
  <div class="card-title">恐惧贪婪指数 {spk_fg}</div>
  <svg class="gauge-svg" viewBox="0 0 260 150">
    <path d="M 20 130 A 110 110 0 0 1 67 33" stroke="#ef4444" stroke-width="14" stroke-linecap="round" fill="none" opacity="0.25"/>
    <path d="M 67 33 A 110 110 0 0 1 130 20" stroke="#f97316" stroke-width="14" stroke-linecap="round" fill="none" opacity="0.25"/>
    <path d="M 130 20 A 110 110 0 0 1 193 33" stroke="#f59e0b" stroke-width="14" stroke-linecap="round" fill="none" opacity="0.25"/>
    <path d="M 193 33 A 110 110 0 0 1 240 130" stroke="#10b981" stroke-width="14" stroke-linecap="round" fill="none" opacity="0.25"/>
    {"<!-- active arc -->" if fg["score"] > 25 else ""}
    <line x1="130" y1="130" x2="{nx}" y2="{ny}" stroke="white" stroke-width="2.5" stroke-linecap="round"/>
    <circle cx="130" cy="130" r="7" fill="#1a2235" stroke="white" stroke-width="2"/>
    <text x="14" y="148" fill="#ef4444" font-size="9" font-family="sans-serif">极度恐惧</text>
    <text x="100" y="13" fill="#f59e0b" font-size="9" font-family="sans-serif">中性</text>
    <text x="200" y="148" fill="#10b981" font-size="9" font-family="sans-serif" text-anchor="end">极度贪婪</text>
  </svg>
  <div style="text-align:center;">
    <div class="big" style="color:{fg_color};" id="fg-num">{fg["score"]}</div>
    <span class="badge {fg_cls}" style="background:{fg_color}22;color:{fg_color};">{fg_label}</span>
    <div class="sub">前日: {fg["prev"]} · {"↓" if fg["score"] < fg["prev"] else "↑"} {abs(fg["score"]-fg["prev"])} pts</div>
  </div>
</div>

<!-- AAII -->
<div class="card yellow col-4">
  <div class="card-label">AAII 散户情绪调查 · 截至 {aaii["week"]}</div>
  <div class="card-title">个人投资者情绪分布</div>
  <div class="bar-row">
    <div class="bar-head">
      <span style="color:var(--muted);">🐂 看涨</span>
      <span style="color:var(--green);font-weight:700;">{aaii["bullish"]}% <small style="color:var(--muted);font-weight:400">均值37.5%</small></span>
    </div>
    <div class="bar-track">
      <div class="bar-fill" style="width:{aaii["bullish"]}%;background:var(--green);"></div>
      <div class="avg-line" style="left:37.5%"></div>
    </div>
  </div>
  <div class="bar-row">
    <div class="bar-head">
      <span style="color:var(--muted);">➡️ 中性</span>
      <span style="color:var(--yellow);font-weight:700;">{aaii["neutral"]}% <small style="color:var(--muted);font-weight:400">均值31.0%</small></span>
    </div>
    <div class="bar-track">
      <div class="bar-fill" style="width:{aaii["neutral"]}%;background:var(--yellow);"></div>
      <div class="avg-line" style="left:31%"></div>
    </div>
  </div>
  <div class="bar-row">
    <div class="bar-head">
      <span style="color:var(--muted);">🐻 看跌</span>
      <span style="color:var(--red);font-weight:700;">{aaii["bearish"]}% <small style="color:var(--muted);font-weight:400">均值31.5%</small></span>
    </div>
    <div class="bar-track">
      <div class="bar-fill" style="width:{aaii["bearish"]}%;background:var(--red);"></div>
      <div class="avg-line" style="left:31.5%"></div>
    </div>
  </div>
  <div style="font-size:0.68rem;color:var(--muted);margin-top:4px;">▏白线 = 历史均值</div>
  <div class="insight">
    {"<strong style='color:var(--green)'>逆向信号：</strong>看跌 &gt; 37% 通常为中期买点（历史统计）" if aaii["bearish"] > 37 else
     "<strong style='color:var(--green)'>看涨情绪积极：</strong>散户乐观情绪高于均值" if aaii["bullish"] > 45 else
     "情绪分布接近历史均值，无极端信号"}
  </div>
</div>

<!-- VIX -->
<div class="card blue col-4">
  <div class="card-label">CBOE · VIX 恐慌指数</div>
  <div class="card-title">市场波动率预期 {spk_vix}</div>
  <div class="big" style="color:{vix_color};">{vix["close"]}</div>
  <div class="sub" style="margin-bottom:10px;">
    {"↓" if vix_chg < 0 else "↑"} {abs(vix_chg)}% · <span style="color:{vix_color};">{vix_level_label}</span>
  </div>
  <div class="range-wrap">
    <div class="range-labels"><span>52W低 {vix["low52"]}</span><span>52W高 {vix["high52"]}</span></div>
    <div class="range-track">
      <div class="range-dot" style="left:{vix_marker_pct}%;background:white;"></div>
    </div>
  </div>
  <div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:10px;font-size:0.75rem;">
    <span style="color:var(--green);">● &lt;15 低</span>
    <span style="color:var(--yellow);">● 15-25 正常</span>
    <span style="color:var(--orange);">● 25-35 高</span>
    <span style="color:var(--red);">● &gt;35 恐慌</span>
  </div>
  <div class="insight">
    {"<strong>低波动：</strong>机构对冲盘未买保护，与散户恐惧情绪形成<strong style='color:var(--yellow)'>分歧</strong>" if vix["close"] < 20 else
     "<strong style='color:var(--orange)'>波动升温：</strong>期权市场开始定价风险，保持警惕" if vix["close"] < 35 else
     "<strong style='color:var(--red)'>恐慌状态：</strong>市场极度紧张，关注流动性风险"}
  </div>
</div>

<!-- SPX -->
<div class="card green col-4">
  <div class="card-label">S&amp;P 500 · 昨日收盘</div>
  <div class="card-title">标普500 {spk_spx}</div>
  <div class="big" style="color:{spx_color};">{spx["close"]:,.0f}</div>
  <div style="margin-top:4px;">
    <span style="background:{spx_color}22;color:{spx_color};padding:3px 10px;border-radius:6px;font-size:0.85rem;font-weight:700;">
      {spx_sign}{spx["chg_pct"]}%
    </span>
    <span class="sub" style="margin-left:8px;">前日 {spx["prev"]:,.0f}</span>
  </div>
  <div class="insight" style="margin-top:14px;">
    {"距全时高点 7,799 约 " + str(round((7799-spx["close"])/7799*100,1)) + "%，强势区间内" if spx["close"] < 7799 else "突破历史高点！"}
  </div>
</div>

<!-- Polymarket -->
<div class="card green col-8">
  <div class="card-label">Polymarket 预测市场 · 资金背书概率</div>
  <div class="card-title">实时赔率</div>
  <div class="poly-item">
    <div class="poly-q">📅 SPX <strong>今日</strong>收涨</div>
    <div class="poly-v" style="color:{poly_color};">{poly["up"]}% Up
      <span class="tag {'tag-bull' if poly['up']>=60 else 'tag-bear' if poly['up']<=40 else ''}">{
        '强偏多' if poly['up']>=65 else '偏多' if poly['up']>=55 else '中性' if poly['up']>=45 else '偏空'
      }</span>
    </div>
  </div>
  <div class="poly-item">
    <div class="poly-q">📈 S&P500 年末 &gt;$6,000</div>
    <div class="poly-v" style="color:var(--green);">58–64%</div>
  </div>
  <div class="poly-item">
    <div class="poly-q">📉 年内跌超 20%（熊市）</div>
    <div class="poly-v" style="color:var(--red);">18–24% <span class="tag tag-bear">低概率</span></div>
  </div>
  <div class="poly-item">
    <div class="poly-q">🏦 2026年衰退</div>
    <div class="poly-v" style="color:var(--yellow);">14%</div>
  </div>
  <div class="poly-item">
    <div class="poly-q">✂️ 2026全年降息 = 0次</div>
    <div class="poly-v" style="color:var(--orange);">95% <span class="tag tag-warn">压制因素</span></div>
  </div>
</div>

<!-- 3-column summary -->
<div class="card multi col-12">
  <div class="card-label">综合结论</div>
  <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;">
    <div style="background:rgba(16,185,129,.06);border:1px solid rgba(16,185,129,.2);border-radius:8px;padding:14px;">
      <div style="color:var(--green);font-weight:700;margin-bottom:6px;">🟢 利多信号</div>
      <ul style="font-size:0.78rem;color:var(--muted);line-height:1.9;padding-left:14px;">
        <li>价格强（距高点 &lt;5%）</li>
        <li>VIX 低位，机构不慌</li>
        <li>散户过度悲观（逆向）</li>
        <li>衰退概率仅 14%</li>
      </ul>
    </div>
    <div style="background:rgba(239,68,68,.06);border:1px solid rgba(239,68,68,.2);border-radius:8px;padding:14px;">
      <div style="color:var(--red);font-weight:700;margin-bottom:6px;">🔴 利空风险</div>
      <ul style="font-size:0.78rem;color:var(--muted);line-height:1.9;padding-left:14px;">
        <li>Fed 9/16 加息 25bp</li>
        <li>全年零降息概率 95%</li>
        <li>F&G 仍在恐惧区</li>
        <li>熊市概率 ~20%</li>
      </ul>
    </div>
    <div style="background:rgba(59,130,246,.06);border:1px solid rgba(59,130,246,.2);border-radius:8px;padding:14px;">
      <div style="color:var(--blue);font-weight:700;margin-bottom:6px;">🔵 核心矛盾</div>
      <ul style="font-size:0.78rem;color:var(--muted);line-height:1.9;padding-left:14px;">
        <li>短线情绪恐惧 × 长线押涨</li>
        <li>散户悲观 × 机构不防守</li>
        <li>加息落地 → 市场反弹</li>
        <li>"爬忧虑之墙"模式</li>
      </ul>
    </div>
  </div>
</div>

</div>

<div class="footer">
  <span>数据来源：CNN Fear &amp; Greed · AAII Sentiment Survey · CBOE VIX · Polymarket · Yahoo Finance</span>
  <span>仅供参考，不构成投资建议</span>
</div>

<script>
function animateValue(el, start, end, dur) {{
  let t0 = null;
  function step(ts) {{
    if (!t0) t0 = ts;
    const p = Math.min((ts - t0) / dur, 1);
    el.textContent = Math.floor(p * (end - start) + start);
    if (p < 1) requestAnimationFrame(step);
  }}
  requestAnimationFrame(step);
}}
const n = document.getElementById('fg-num');
if (n) animateValue(n, 0, {fg["score"]}, 1200);
document.querySelectorAll('.bar-fill').forEach(b => {{
  const w = b.style.width; b.style.width = '0';
  setTimeout(() => b.style.width = w, 300);
}});
</script>
</body>
</html>"""

# ─────────────────────────────────────────────
# 3b. 更新 v2 HTML（保留样式，只替换数据）
# ─────────────────────────────────────────────

def update_v2_html(fg, vix, spx, aaii, poly, history, now, mode):
    import re

    # 读取现有 v2 HTML：
    # CI → checkout 的 index.html；本地 → us_sentiment_dashboard_v2.html
    src = OUTPUT_HTML if _IS_CI else "/Users/admin/us_sentiment_dashboard_v2.html"
    if not os.path.exists(src):
        src = OUTPUT_HTML  # 最终兜底
    with open(src, encoding="utf-8") as f:
        html = f.read()

    fg_color, fg_label, _ = rating_color(fg["score"])
    vix_color, _          = vix_level(vix["close"])[:2]
    spx_color = "#10b981" if spx["chg_pct"] >= 0 else "#ef4444"
    spx_sign  = "+" if spx["chg_pct"] >= 0 else ""
    poly_color = "#10b981" if poly["up"] >= 55 else "#ef4444" if poly["up"] <= 45 else "#f59e0b"

    # 1. 标题日期
    zh_weekday = ["周一","周二","周三","周四","周五","周六","周日"][now.weekday()]
    date_str_zh = now.strftime(f"%Y年%-m月%-d日")
    html = re.sub(r'<strong>\d{4}年\d+月\d+日</strong>',
                  f'<strong>{date_str_zh}</strong>', html)
    html = re.sub(r'周[一二三四五六日] · 美东时间',
                  f'{zh_weekday} · 美东时间', html)

    # 2. summary-card 整体情绪
    html = re.sub(
        r'(<div class="summary-label">整体情绪</div>\s*<div class="summary-val"[^>]*>)[^<]*(</div>)',
        rf'\g<1><span style="color:{fg_color};">{fg_label}</span>\g<2>', html)

    # 3. summary-card SPX 昨收
    html = re.sub(
        r'(<div class="summary-label">SPX 昨收</div>\s*<div class="summary-val"[^>]*>)[^<]*(</div>)',
        rf'\g<1>{spx["close"]:,.2f}\g<2>', html)

    # 4. summary-card 今日涨概率
    html = re.sub(
        r'(<div class="summary-label">今日涨概率</div>\s*<div class="summary-val"[^>]*>)[^<]*(</div>)',
        rf'\g<1>{poly["up"]}%\g<2>', html)

    # 5. summary-card VIX
    html = re.sub(
        r'(<div class="summary-label">VIX</div>\s*<div class="summary-val"[^>]*>)[^<]*(</div>)',
        rf'\g<1>{vix["close"]}\g<2>', html)

    # 6. F&G 大数字
    html = re.sub(r'(<div class="gauge-value"[^>]*id="fg-num"[^>]*>)\d+(</div>)',
                  rf'\g<1>{fg["score"]}\g<2>', html)
    html = re.sub(r'animateValue\(fgNum, 0, \d+,',
                  f'animateValue(fgNum, 0, {fg["score"]},', html)

    # 7. AAII 进度条宽度
    html = re.sub(r'(🐂 看涨.*?)([\d.]+)(%.*?均值)', lambda m:
        m.group(0).replace(m.group(2), str(aaii["bullish"])), html, flags=re.DOTALL)
    for emoji, key, color in [("🐻", "bearish", "--red"), ("➡️", "neutral", "--yellow")]:
        pass  # minimal change: just update bar widths via data attrs if needed

    # 8. VIX 大数字（class="vix-big"）
    html = re.sub(r'(<div class="vix-big"[^>]*>)([\d.]+)(</div>)',
                  rf'\g<1>{vix["close"]}\g<3>', html)

    # 8a. VIX 卡片标签日期更新（CBOE · M/D 实时）
    vix_date_label = now.strftime("%-m/%-d")
    html = re.sub(r'(CBOE · )\d+/\d+( 实时)', rf'\g<1>{vix_date_label}\g<2>', html)

    # 8b. VIX 前收/涨跌描述
    vix_prev = vix.get("prev", vix["close"])
    vix_chg = vix["close"] - vix_prev if vix_prev else 0
    vix_chg_pct = (vix_chg / vix_prev * 100) if vix_prev else 0
    vix_arrow = "↑" if vix_chg >= 0 else "↓"
    vix_level_text = vix_level(vix["close"])[1]
    html = re.sub(
        r'(较前日\s*)[↓↑]?\s*[\d.]+%.*?(?=</div>)',
        rf'\g<1>{vix_arrow} {abs(vix_chg_pct):.2f}% · 前收 {vix_prev:.2f} · {vix_level_text}',
        html)

    # 8c. VIX range marker 位置
    vix_low52 = 13.38
    vix_high52 = 35.30
    marker_pct = (vix["close"] - vix_low52) / (vix_high52 - vix_low52) * 100
    marker_pct = max(0, min(100, marker_pct))
    html = re.sub(r'(class="range-marker"[^>]*style="left:)[\d.]+%',
                  rf'\g<1>{marker_pct:.1f}%', html)

    # 9. 图表数据从 history 重建
    if len(history) >= 1:
        labels_js  = json.dumps([h["date"][5:].lstrip("0").replace("-0","/").replace("-","/") for h in history])
        fg_js      = json.dumps([h.get("fg_score") for h in history])
        vix_js     = json.dumps([h.get("vix_close") for h in history])
        spxchg_js  = json.dumps([h.get("spx_chg") for h in history])
        html = re.sub(r'const labels\s*=\s*\[.*?\];', f'const labels  = {labels_js};', html, flags=re.DOTALL)
        html = re.sub(r'const fg\s*=\s*\[.*?\];',     f'const fg      = {fg_js};',     html, flags=re.DOTALL)
        html = re.sub(r'const vix\s*=\s*\[.*?\];',    f'const vix     = {vix_js};',    html, flags=re.DOTALL)
        html = re.sub(r'const spxChg\s*=\s*\[.*?\];', f'const spxChg  = {spxchg_js};', html, flags=re.DOTALL)

    # 10. 页脚更新时间（完全替换，不追加）
    html = re.sub(r'更新时间：[^·]*?(?=</span>)',
                  f'更新时间：{now.strftime("%Y-%m-%d %H:%M ET")}', html)

    return html


# ─────────────────────────────────────────────
# 4. 主程序
# ─────────────────────────────────────────────

def main():
    et = pytz.timezone("America/New_York")
    now = datetime.now(et)
    now_str = now.strftime("%H:%M")

    # 运行模式：
    #   --post-market  → 盘后，只更新 SPX 收盘涨跌幅
    #   默认（盘前）   → 更新 F&G / VIX / AAII / Polymarket
    mode = "post" if "--post-market" in sys.argv else "pre"
    print(f"[{now_str} ET] 模式: {'盘后 SPX 收盘更新' if mode=='post' else '盘前情绪数据更新'}")

    history = load_history()
    date_str = date.today().isoformat()
    # 取今日已有记录作为基础（盘后更新时保留盘前数据）
    today_base = next((h for h in history if h["date"] == date_str), {})

    if mode == "post":
        # 盘后：只抓 SPX 收盘
        spx_new = fetch_spx()
        last_good = history[-1] if history else {}
        # fallback 不上线，保留上次已知正确值
        if spx_new != FALLBACKS["spx"]:
            spx = spx_new
            print(f"  SPX 收盘: {spx['close']} ({spx['chg_pct']:+}%)")
        else:
            spx = {**FALLBACKS["spx"], "close": last_good.get("spx_close", FALLBACKS["spx"]["close"]), "chg_pct": last_good.get("spx_chg", 0.0)}
            print(f"  SPX 收盘: 获取失败，保留上次已知值 {spx['close']}  [保留上次]")
        today_data = {**today_base, "spx_close": spx["close"], "spx_chg": spx["chg_pct"]}
        # 重新读取其他字段用于渲染 HTML
        fg   = {"score": today_base.get("fg_score", 29), "rating": today_base.get("fg_rating", "Fear"), "prev": 31}
        vix  = {"close": today_base.get("vix_close", 15.84), "prev": 17.84, "high52": 35.30, "low52": 13.38}
        aaii = {"bullish": today_base.get("aaii_bull", 38.0), "neutral": today_base.get("aaii_neutral", 22.7),
                "bearish": today_base.get("aaii_bear", 39.3), "week": today_base.get("aaii_week", "—")}
        poly = {"up": today_base.get("poly_up", 50), "down": 100 - today_base.get("poly_up", 50), "found": False}
        # post 模式不抓其他指标，给空值供 fallback_flags 使用
        fg_new, vix_new, aaii_new, poly_new = FALLBACKS["fg"], FALLBACKS["vix"], FALLBACKS["aaii"], FALLBACKS["poly"]
    else:
        # 盘前：更新情绪指标，SPX 取前日收盘
        fg_new   = fetch_fear_greed()
        vix_new  = fetch_vix()
        spx_new  = fetch_spx()
        aaii_new = fetch_aaii()
        poly_new = fetch_polymarket_spx_today()

        # ── 核心原则：fallback 值不覆盖已知正确数据 ──────────────────
        # 取上一条历史记录作为"已知正确值"兜底
        last_good = history[-1] if history else {}

        def _use_real(new_val, fallback_val, last_key, last_hist):
            """若新值 == fallback，保留上次已知值；否则用新值"""
            return new_val if new_val != fallback_val else last_hist.get(last_key, new_val)

        fg   = fg_new   if fg_new   != FALLBACKS["fg"]   else {"score": last_good.get("fg_score", FALLBACKS["fg"]["score"]), "rating": last_good.get("fg_rating", FALLBACKS["fg"]["rating"]), "prev": FALLBACKS["fg"]["prev"]}
        vix  = vix_new  if vix_new  != FALLBACKS["vix"]  else {**FALLBACKS["vix"],  "close": last_good.get("vix_close", FALLBACKS["vix"]["close"])}
        spx  = spx_new  if spx_new  != FALLBACKS["spx"]  else {**FALLBACKS["spx"],  "close": last_good.get("spx_close", FALLBACKS["spx"]["close"]), "chg_pct": last_good.get("spx_chg", 0.0)}
        aaii = aaii_new if aaii_new != FALLBACKS["aaii"] else {"bullish": last_good.get("aaii_bull", FALLBACKS["aaii"]["bullish"]), "neutral": last_good.get("aaii_neutral", FALLBACKS["aaii"]["neutral"]), "bearish": last_good.get("aaii_bear", FALLBACKS["aaii"]["bearish"]), "week": last_good.get("aaii_week", "—")}
        poly = poly_new if poly_new.get("found") else {"up": last_good.get("poly_up", FALLBACKS["poly"]["up"]), "down": 100 - last_good.get("poly_up", FALLBACKS["poly"]["up"]), "found": False}

        print(f"  F&G: {fg['score']} ({fg['rating']}){'  [保留上次]' if fg_new==FALLBACKS['fg'] else ''}")
        print(f"  VIX: {vix['close']}{'  [保留上次]' if vix_new==FALLBACKS['vix'] else ''}")
        print(f"  SPX(前日): {spx['close']} ({spx['chg_pct']:+}%){'  [保留上次]' if spx_new==FALLBACKS['spx'] else ''}")
        print(f"  AAII: Bull {aaii['bullish']}% | Bear {aaii['bearish']}%")
        print(f"  Polymarket: {poly['up']}%{'  [保留上次]' if not poly_new.get('found') else ''}")

        today_data = {
            **today_base,
            "fg_score":     fg["score"],
            "fg_rating":    fg["rating"],
            "vix_close":    vix["close"],
            "spx_close":    today_base.get("spx_close", spx["close"]),
            "spx_chg":      today_base.get("spx_chg",   spx["chg_pct"]),
            "aaii_bull":    aaii["bullish"],
            "aaii_neutral": aaii["neutral"],
            "aaii_bear":    aaii["bearish"],
            "aaii_week":    aaii["week"],
            "poly_up":      poly["up"],
        }

    history = load_history()
    history = save_history(history, today_data)

    html = update_v2_html(fg, vix, spx, aaii, poly, history, now, mode)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"✅ 仪表盘已更新: {OUTPUT_HTML}")

    # 校验：任何指标 fallback 都告警（不只看全量）
    fallback_flags = {
        "F&G":       fg_new == FALLBACKS["fg"]   if mode == "pre" else False,
        "VIX":       vix_new == FALLBACKS["vix"] if mode == "pre" else False,
        "SPX":       spx_new == FALLBACKS["spx"],
        "AAII":      aaii_new == FALLBACKS["aaii"] if mode == "pre" else False,
        "Polymarket": not poly_new.get("found")    if mode == "pre" else False,
    }
    is_fallback = any(fallback_flags.values())
    for k, v in fallback_flags.items():
        if v:
            print(f"  ⚠️  {k} 使用了 fallback / 保留上次值 → 数据源异常")
    if all(fallback_flags.values()):
        print("⚠️  警告：所有数据源均不可用，请人工检查")

    # 写入状态文件（本地 & CI 均写）
    status = {
        "last_run":  datetime.now(pytz.timezone("America/New_York")).isoformat(),
        "mode":      mode,
        "data_ok":   not is_fallback,
        "fg_score":  fg["score"],
        "vix_close": vix["close"],
        "spx_close": spx["close"],
        "spx_chg":   spx["chg_pct"],
        "poly_up":   poly["up"],
    }
    with open(STATUS_FILE, "w") as f:
        json.dump(status, f, indent=2)

    # CI 模式：文件已写到工作区，由 workflow 的 git push 步骤统一提交
    # 本地模式：通过 GitHub API 直接推送
    if not _IS_CI:
        push_to_github(html, status)

    # ── Double-check：回验线上页面数据是否与本次推送一致 ──────────────
    import time, re as _re
    PAGES_URL = "https://chenfei0710-source.github.io/us-sentiment-dashboard/"
    print("\n🔍 Double-check：等待 20 秒后回验线上数据...")
    time.sleep(20)
    try:
        live = requests.get(PAGES_URL, timeout=15).text
        checks = {
            "SPX":  (f"{spx['close']:,.2f}", live),
            "VIX":  (str(vix["close"]),       live),
            "F&G":  (str(fg["score"]),         live),
            "日期":  (now.strftime("%Y年%-m月%-d日"), live),
        }
        all_ok = True
        for label, (expected, page) in checks.items():
            if expected in page:
                print(f"  ✅ {label}: {expected} 已在线上确认")
            else:
                print(f"  ❌ {label}: 期望 {expected}，线上未找到 → 可能仍在 CDN 刷新中")
                all_ok = False
        if all_ok:
            print("✅ Double-check 通过，线上数据与本次更新一致")
        else:
            print("⚠️  部分数据尚未反映，建议 1-2 分钟后手动刷新页面确认")
    except Exception as e:
        print(f"  ⚠️  回验请求失败: {e}")

    # ── 飞书通知 ──
    send_feishu_notification(status, fallback_flags, mode)


def _push_file(api_base, headers, path, content_bytes, message):
    """推送单个文件到 GitHub，失败自动重试一次"""
    import base64
    url = f"{api_base}/contents/{path}"
    try:
        r = requests.get(url, headers=headers, timeout=10)
        sha = r.json().get("sha") if r.status_code == 200 else None
        payload = {
            "message": message,
            "content": base64.b64encode(content_bytes).decode(),
            "branch":  "main",
        }
        if sha:
            payload["sha"] = sha
        r2 = requests.put(url, headers=headers, json=payload, timeout=15)
        if r2.status_code in (200, 201):
            print(f"  ✅ {path} 推送成功")
            return True
        else:
            print(f"  ⚠️ {path} 推送失败: {r2.status_code}")
            return False
    except Exception as e:
        print(f"  ⚠️ {path} 推送异常: {e}")
        return False

def push_to_github(html: str, status: dict = None):
    """Push updated dashboard + status.json to GitHub Pages"""
    GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN") or _read_local_gh_token()
    if not GITHUB_TOKEN:
        print("⚠️  GitHub token 未找到，跳过推送")
        return

    GITHUB_USER = os.environ.get("GITHUB_USER", "chenfei0710-source")
    REPO_NAME   = os.environ.get("GITHUB_REPO", "us-sentiment-dashboard")
    API_BASE    = f"https://api.github.com/repos/{GITHUB_USER}/{REPO_NAME}"
    HEADERS     = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept":        "application/vnd.github.v3+json",
        "User-Agent":    "sentiment-tracker",
    }
    msg = f"自动更新 {date.today().isoformat()}"

    ok_html = _push_file(API_BASE, HEADERS, "index.html", html.encode(), msg)

    if status:
        status_bytes = json.dumps(status, indent=2, ensure_ascii=False).encode()
        _push_file(API_BASE, HEADERS, "status.json", status_bytes, msg)

    if ok_html:
        print(f"✅ GitHub Pages 已更新: https://{GITHUB_USER}.github.io/{REPO_NAME}")
    else:
        print("❌ GitHub Pages 更新失败，请检查 token 和网络")


def send_feishu_notification(status: dict, fallback_flags: dict, mode: str):
    """更新完成后推送飞书通知"""
    feishu_app_id     = os.environ.get("FEISHU_APP_ID", "")
    feishu_app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    feishu_open_id    = os.environ.get("FEISHU_USER_OPENID", "")
    if not all([feishu_app_id, feishu_app_secret, feishu_open_id]):
        print("  ℹ️  飞书凭证未设置，跳过推送通知")
        return

    try:
        # 获取 tenant_access_token
        r = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": feishu_app_id, "app_secret": feishu_app_secret},
            timeout=10,
        )
        token = r.json().get("tenant_access_token", "")
        if not token:
            print(f"  ⚠️  飞书 token 获取失败: {r.json()}")
            return

        # 构建消息
        mode_label = "📈 盘前更新" if mode == "pre" else "📊 盘后更新"
        fallback_list = [k for k, v in fallback_flags.items() if v]
        fallback_warn = f"\n⚠️ 数据源异常: {', '.join(fallback_list)}" if fallback_list else ""
        data_ok = "✅ 全部正常" if not fallback_list else "⚠️ 有数据源异常"

        lines = [
            f"🔔 美股情绪仪表盘 · {mode_label}",
            f"",
            f"📅 {status.get('last_run','')[:16]}",
            f"",
            f"😱 F&G 恐惧贪婪: {status.get('fg_score','?')}",
            f"📊 VIX: {status.get('vix_close','?')}",
            f"📈 SPX: {status.get('spx_close','?')} ({status.get('spx_chg','?'):+.2f}%)" if isinstance(status.get('spx_chg'), (int, float)) else f"📈 SPX: {status.get('spx_close','?')}",
            f"🎯 Polymarket 看涨: {status.get('poly_up','?')}%",
            f"",
            f"{data_ok}{fallback_warn}",
            f"",
            f"🔗 https://chenfei0710-source.github.io/us-sentiment-dashboard/",
        ]
        content = json.dumps({"text": "\n".join(lines)})

        r = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"receive_id": feishu_open_id, "msg_type": "text", "content": content},
            timeout=10,
        )
        if r.json().get("code") == 0:
            print("  ✅ 飞书通知已发送")
        else:
            print(f"  ⚠️  飞书通知发送失败: {r.json()}")
    except Exception as e:
        print(f"  ⚠️  飞书推送异常: {e}")


if __name__ == "__main__":
    main()
