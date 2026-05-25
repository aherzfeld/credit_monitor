"""
Credit Cycle Monitor
====================
Pulls credit-stress indicators from FRED and Yahoo Finance, computes a
composite credit-stress score (0-100), renders an HTML dashboard, and
emails an alert when thresholds are crossed.

USAGE
-----
1. Copy config.example.yaml -> config.yaml and fill in your settings.
2. Get a free FRED API key: https://fred.stlouisfed.org/docs/api/api_key.html
3. Install deps: pip install -r requirements.txt
4. Run: python credit_monitor.py

The script writes dashboard.html and (optionally) emails an alert.
Schedule it daily via cron, Windows Task Scheduler, or GitHub Actions.
"""

from __future__ import annotations

import json
import os
import smtplib
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
import yaml
import yfinance as yf


# ---------------------------------------------------------------------------
# Indicator definitions
# ---------------------------------------------------------------------------

@dataclass
class IndicatorResult:
    name: str
    value: float
    score: float            # 0-100 stress sub-score (higher = more stress)
    weight: float
    source: str
    note: str = ""
    history: list = field(default_factory=list)   # list of (date_str, value)
    explanation: str = ""    # tooltip text shown on info button click


def fetch_fred_series(series_id: str, api_key: str, lookback_days: int = 730) -> pd.Series:
    """Fetch a FRED series. Returns a pandas Series indexed by date."""
    start = (datetime.utcnow() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": series_id,
        "api_key": api_key,
        "file_type": "json",
        "observation_start": start,
    }
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    rows = [(o["date"], o["value"]) for o in data["observations"] if o["value"] != "."]
    if not rows:
        raise RuntimeError(f"FRED returned no data for {series_id}")
    df = pd.DataFrame(rows, columns=["date", "value"])
    df["date"] = pd.to_datetime(df["date"])
    df["value"] = pd.to_numeric(df["value"])
    df = df.sort_values("date").set_index("date")
    return df["value"]


def fetch_yahoo(ticker: str, lookback_days: int = 730) -> pd.Series:
    """Fetch adjusted close prices for a Yahoo ticker."""
    start = (datetime.utcnow() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    df = yf.download(ticker, start=start, progress=False, auto_adjust=True)
    if df.empty:
        raise RuntimeError(f"No Yahoo data for {ticker}")
    # yfinance can return MultiIndex columns on newer versions
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df["Close"].dropna()


# ---------------------------------------------------------------------------
# Sub-score logic
# Each helper returns a 0-100 stress sub-score (higher = more stress).
# ---------------------------------------------------------------------------

def score_linear(value: float, benign: float, stressed: float) -> float:
    """Linear scaling. value at `benign` -> 0, at `stressed` -> 100."""
    if stressed == benign:
        return 50.0
    pct = (value - benign) / (stressed - benign)
    return max(0.0, min(100.0, pct * 100))


def score_inverted_linear(value: float, benign: float, stressed: float) -> float:
    """For metrics where LOWER values = MORE stress (e.g. yield curve)."""
    if stressed == benign:
        return 50.0
    pct = (benign - value) / (benign - stressed)
    return max(0.0, min(100.0, pct * 100))


def _series_to_history(s: pd.Series, points: int = 90) -> list:
    tail = s.tail(points)
    return [(d.strftime("%Y-%m-%d"), float(v)) for d, v in tail.items()]


# ---------------------------------------------------------------------------
# Indicator explanations (shown in click-tooltips on the dashboard)
# ---------------------------------------------------------------------------

EXPLANATIONS = {
    "hy_oas": (
        "The spread (in basis points) between yields on US junk bonds and "
        "comparable Treasuries. This is the single most important real-time "
        "credit-stress signal — it reflects what bond investors are actually "
        "demanding to take on default risk. Spreads widen BEFORE defaults "
        "actually rise, so it's forward-looking. Rough guide: under 350bps is "
        "complacent, 400–500 is normal, 600+ signals stress, 1000+ is crisis."
    ),
    "ig_oas": (
        "The spread between investment-grade corporate bonds and Treasuries. "
        "Less reactive than HY OAS, but when IG spreads widen meaningfully it "
        "means stress is spreading from speculative borrowers into "
        "blue-chip credit — a more serious signal. Used as confirmation "
        "of broader credit deterioration."
    ),
    "yield_curve": (
        "The difference between 10-year and 2-year Treasury yields. When "
        "short rates exceed long rates (inversion), it's historically "
        "preceded every US recession in modern history. The Fed's tight "
        "policy is squeezing the economy. Counterintuitively, the "
        "DIS-inversion (curve steepening back to positive after being "
        "inverted) is often the actual recession trigger."
    ),
    "sloos": (
        "Senior Loan Officer Opinion Survey — net % of US banks reporting "
        "TIGHTER lending standards on commercial & industrial loans, "
        "released quarterly by the Fed. When banks pull back on lending, "
        "credit availability shrinks across the economy. Widespread "
        "tightening (>10%) typically precedes recession by 6–12 months. "
        "Especially relevant to bank holdings like KB, NU, BLX."
    ),
    "cc_delinq": (
        "% of credit card loan balances 30+ days past due across all US "
        "commercial banks. This is the consumer's canary in the coal mine "
        "— rising delinquencies mean households are running out of cushion. "
        "Watch the TREND more than the level: an accelerating rise from "
        "a low base is more alarming than a high but flat reading."
    ),
    "kre_spy": (
        "Regional bank ETF (KRE) performance vs. S&P 500 (SPY) over 3 months. "
        "Banks are sensitive to credit conditions, deposit flight, and "
        "commercial real estate exposure. When bank stocks meaningfully "
        "underperform the broad market, equity investors are sniffing out "
        "credit problems before they show up in lagging economic data."
    ),
    "emb": (
        "Emerging-market sovereign debt ETF (EMB) price change over 6 months. "
        "When global risk appetite contracts or the USD strengthens, EM "
        "sovereigns get hit first. Especially relevant for your LatAm "
        "holdings (NU, BLX) — EM stress is often a leading indicator of "
        "broader credit cycle turns."
    ),
    "hyg_lqd": (
        "Ratio of high-yield bond ETF (HYG) to investment-grade ETF (LQD). "
        "When this ratio falls, investors are dumping junk and crowding "
        "into safer credit — a flight-to-quality signal. It's a faster, "
        "more responsive proxy for HY OAS that updates intraday."
    ),
}


# ---------------------------------------------------------------------------
# Build indicators
# ---------------------------------------------------------------------------

def build_indicators(cfg: dict) -> list[IndicatorResult]:
    api_key = cfg["fred_api_key"]
    results: list[IndicatorResult] = []

    # 1. High-yield OAS — the single most important credit signal
    try:
        s = fetch_fred_series("BAMLH0A0HYM2", api_key)
        v = float(s.iloc[-1])
        # 300bps benign, 1000bps crisis-level
        score = score_linear(v, benign=300, stressed=1000)
        results.append(IndicatorResult(
            name="High-Yield OAS",
            value=v,
            score=score,
            weight=0.25,
            source="FRED: BAMLH0A0HYM2",
            note=f"{v:.0f} bps — junk bond risk premium",
            history=_series_to_history(s),
            explanation=EXPLANATIONS["hy_oas"],
        ))
    except Exception as e:
        print(f"[WARN] HY OAS failed: {e}", file=sys.stderr)

    # 2. Investment-grade OAS
    try:
        s = fetch_fred_series("BAMLC0A0CM", api_key)
        v = float(s.iloc[-1])
        score = score_linear(v, benign=80, stressed=300)
        results.append(IndicatorResult(
            name="Investment-Grade OAS",
            value=v,
            score=score,
            weight=0.10,
            source="FRED: BAMLC0A0CM",
            note=f"{v:.0f} bps — corporate spread",
            history=_series_to_history(s),
            explanation=EXPLANATIONS["ig_oas"],
        ))
    except Exception as e:
        print(f"[WARN] IG OAS failed: {e}", file=sys.stderr)

    # 3. 2s10s yield curve. Inversion historically precedes recession.
    try:
        s = fetch_fred_series("T10Y2Y", api_key)
        v = float(s.iloc[-1])
        # +1.5% benign, -1.0% stressed (deep inversion)
        score = score_inverted_linear(v, benign=1.5, stressed=-1.0)
        results.append(IndicatorResult(
            name="2s10s Yield Curve",
            value=v,
            score=score,
            weight=0.15,
            source="FRED: T10Y2Y",
            note=f"{v:.2f}% — {'inverted' if v < 0 else 'positive'}",
            history=_series_to_history(s),
            explanation=EXPLANATIONS["yield_curve"],
        ))
    except Exception as e:
        print(f"[WARN] 2s10s failed: {e}", file=sys.stderr)

    # 4. Senior Loan Officer Survey — net % tightening C&I lending standards
    try:
        s = fetch_fred_series("DRTSCILM", api_key)
        v = float(s.iloc[-1])
        score = score_linear(v, benign=-10, stressed=50)
        results.append(IndicatorResult(
            name="SLOOS C&I Tightening",
            value=v,
            score=score,
            weight=0.15,
            source="FRED: DRTSCILM (quarterly)",
            note=f"{v:.1f}% net tightening",
            history=_series_to_history(s),
            explanation=EXPLANATIONS["sloos"],
        ))
    except Exception as e:
        print(f"[WARN] SLOOS failed: {e}", file=sys.stderr)

    # 5. Credit card delinquency rate
    try:
        s = fetch_fred_series("DRCCLACBS", api_key)
        v = float(s.iloc[-1])
        score = score_linear(v, benign=2.0, stressed=6.0)
        results.append(IndicatorResult(
            name="Credit Card Delinquency",
            value=v,
            score=score,
            weight=0.10,
            source="FRED: DRCCLACBS",
            note=f"{v:.2f}% — consumer credit health",
            history=_series_to_history(s),
            explanation=EXPLANATIONS["cc_delinq"],
        ))
    except Exception as e:
        print(f"[WARN] Credit card delinq failed: {e}", file=sys.stderr)

    # 6. Regional banks vs SPY (3-month relative performance)
    try:
        kre = fetch_yahoo("KRE")
        spy = fetch_yahoo("SPY")
        ratio = (kre / spy).dropna()
        # 3-month % change in the ratio. Negative = banks underperforming.
        recent = ratio.iloc[-1]
        three_mo_ago = ratio.iloc[-63] if len(ratio) > 63 else ratio.iloc[0]
        pct_change = (recent / three_mo_ago - 1) * 100
        # +5% benign, -20% stressed
        score = score_inverted_linear(float(pct_change), benign=5.0, stressed=-20.0)
        results.append(IndicatorResult(
            name="KRE/SPY 3-Month",
            value=float(pct_change),
            score=score,
            weight=0.10,
            source="Yahoo: KRE, SPY",
            note=f"{pct_change:+.1f}% — bank relative perf",
            history=_series_to_history(ratio),
            explanation=EXPLANATIONS["kre_spy"],
        ))
    except Exception as e:
        print(f"[WARN] KRE/SPY failed: {e}", file=sys.stderr)

    # 7. EM debt — proxy via EMB ETF price decline (lower price = wider spreads)
    try:
        emb = fetch_yahoo("EMB")
        recent = emb.iloc[-1]
        six_mo_ago = emb.iloc[-126] if len(emb) > 126 else emb.iloc[0]
        pct_change = (recent / six_mo_ago - 1) * 100
        score = score_inverted_linear(float(pct_change), benign=3.0, stressed=-15.0)
        results.append(IndicatorResult(
            name="EM Debt (EMB) 6-Month",
            value=float(pct_change),
            score=score,
            weight=0.10,
            source="Yahoo: EMB",
            note=f"{pct_change:+.1f}% — EM sovereign debt",
            history=_series_to_history(emb),
            explanation=EXPLANATIONS["emb"],
        ))
    except Exception as e:
        print(f"[WARN] EMB failed: {e}", file=sys.stderr)

    # 8. HYG/LQD ratio — junk vs investment-grade. Falling = stress.
    try:
        hyg = fetch_yahoo("HYG")
        lqd = fetch_yahoo("LQD")
        ratio = (hyg / lqd).dropna()
        recent = ratio.iloc[-1]
        three_mo_ago = ratio.iloc[-63] if len(ratio) > 63 else ratio.iloc[0]
        pct_change = (recent / three_mo_ago - 1) * 100
        score = score_inverted_linear(float(pct_change), benign=2.0, stressed=-10.0)
        results.append(IndicatorResult(
            name="HYG/LQD 3-Month",
            value=float(pct_change),
            score=score,
            weight=0.05,
            source="Yahoo: HYG, LQD",
            note=f"{pct_change:+.1f}% — junk vs IG",
            history=_series_to_history(ratio),
            explanation=EXPLANATIONS["hyg_lqd"],
        ))
    except Exception as e:
        print(f"[WARN] HYG/LQD failed: {e}", file=sys.stderr)

    return results


# ---------------------------------------------------------------------------
# Composite scoring
# ---------------------------------------------------------------------------

def composite_score(indicators: list[IndicatorResult]) -> float:
    """Weighted average. Renormalizes if any indicators failed to load."""
    total_weight = sum(i.weight for i in indicators)
    if total_weight == 0:
        return 0.0
    weighted = sum(i.score * i.weight for i in indicators)
    return weighted / total_weight


def append_history(score: float, history_path: Path) -> list[tuple[str, float]]:
    """Append today's composite score to history CSV. Returns the full history."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    history: list[tuple[str, float]] = []

    if history_path.exists():
        for line in history_path.read_text().strip().splitlines():
            if not line or line.startswith("date,"):
                continue
            d, v = line.split(",")
            history.append((d, float(v)))

    # If we already have today's entry, overwrite it; otherwise append.
    history = [(d, v) for d, v in history if d != today]
    history.append((today, score))
    history.sort(key=lambda x: x[0])

    # Keep last 730 days only
    history = history[-730:]

    lines = ["date,composite"] + [f"{d},{v:.2f}" for d, v in history]
    history_path.write_text("\n".join(lines) + "\n")
    return history


def stress_band(score: float) -> tuple[str, str, str]:
    """Return (label, hex_color, action_text) for a composite score."""
    if score < 25:
        return ("Benign", "#3a7d44", "Credit conditions are calm. Stay invested.")
    elif score < 50:
        return ("Normal", "#c9a227", "Routine. Monitor weekly.")
    elif score < 75:
        return ("Tightening", "#d96704", "Credit deteriorating. Review cyclical exposure.")
    else:
        return ("Stressed", "#b3001b", "Defensive positioning warranted. Trim risk.")


# ---------------------------------------------------------------------------
# HTML dashboard
# ---------------------------------------------------------------------------

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Credit Cycle Monitor</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Libre+Caslon+Text:ital,wght@0,400;0,700;1,400&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg: #0f0e0c;
    --panel: #181613;
    --panel-2: #221f1b;
    --ink: #f2eee7;
    --ink-dim: #a09686;
    --rule: #2e2a25;
    --accent: {accent};
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--bg);
    color: var(--ink);
    font-family: 'Libre Caslon Text', Georgia, serif;
    font-feature-settings: "ss01" on, "ss02" on;
    line-height: 1.5;
    min-height: 100vh;
  }}
  .grain {{
    position: fixed; inset: 0; pointer-events: none; opacity: .035;
    background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='180' height='180'><filter id='n'><feTurbulence baseFrequency='0.9'/></filter><rect width='100%' height='100%' filter='url(%23n)'/></svg>");
    mix-blend-mode: screen;
    z-index: 100;
  }}
  .wrap {{ max-width: 1180px; margin: 0 auto; padding: 56px 32px 96px; }}
  header {{
    display: flex; justify-content: space-between; align-items: baseline;
    border-bottom: 1px solid var(--rule); padding-bottom: 20px; margin-bottom: 48px;
  }}
  h1 {{
    font-family: 'Libre Caslon Text', serif;
    font-weight: 700;
    font-size: 42px;
    letter-spacing: -0.01em;
    margin: 0;
  }}
  h1 em {{ font-style: italic; color: var(--accent); font-weight: 400; }}
  .timestamp {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px;
    color: var(--ink-dim);
    letter-spacing: 0.05em;
    text-transform: uppercase;
  }}
  .hero {{
    display: grid; grid-template-columns: 1.2fr 1fr; gap: 32px;
    margin-bottom: 56px;
    align-items: stretch;
  }}
  .score-card {{
    background: var(--panel);
    border: 1px solid var(--rule);
    padding: 40px;
    position: relative;
    overflow: hidden;
  }}
  .score-card::before {{
    content: ""; position: absolute; top: 0; left: 0; right: 0; height: 3px;
    background: var(--accent);
  }}
  .score-label {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px; letter-spacing: 0.18em; text-transform: uppercase;
    color: var(--ink-dim);
    margin-bottom: 16px;
  }}
  .score-big {{
    font-family: 'Libre Caslon Text', serif;
    font-size: 140px; line-height: 0.9; font-weight: 400;
    letter-spacing: -0.03em;
    margin: 0;
  }}
  .score-big sup {{
    font-size: 28px;
    color: var(--ink-dim);
    top: -68px;
    left: 18px;
    margin-left: 4px;
    letter-spacing: 0.02em;
    font-weight: 400;
  }}
  .score-band {{
    margin-top: 16px;
    font-size: 28px; font-style: italic;
    color: var(--accent);
    font-weight: 600;
  }}
  .score-action {{
    margin-top: 20px;
    font-size: 16px; color: var(--ink-dim);
    max-width: 360px;
  }}
  .legend {{
    background: var(--panel-2);
    border: 1px solid var(--rule);
    padding: 32px;
  }}
  .legend h2 {{
    font-family: 'JetBrains Mono', monospace; font-size: 11px;
    letter-spacing: 0.18em; text-transform: uppercase;
    color: var(--ink-dim); margin: 0 0 20px;
  }}
  .band-row {{ display: flex; align-items: center; gap: 16px; padding: 10px 0; border-bottom: 1px dotted var(--rule); }}
  .band-row:last-child {{ border-bottom: none; }}
  .band-dot {{ width: 12px; height: 12px; border-radius: 50%; }}
  .band-range {{ font-family: 'JetBrains Mono', monospace; font-size: 12px; color: var(--ink-dim); min-width: 56px; }}
  .band-name {{ font-size: 18px; flex: 1; }}
  .band-name em {{ font-style: italic; color: var(--ink-dim); }}
  .trend-section {{
    background: var(--panel);
    border: 1px solid var(--rule);
    padding: 32px;
    margin-bottom: 56px;
  }}
  .trend-section h2 {{
    font-family: 'JetBrains Mono', monospace; font-size: 11px;
    letter-spacing: 0.18em; text-transform: uppercase;
    color: var(--ink-dim); margin: 0 0 24px;
  }}
  .trend-chart {{ width: 100%; height: auto; display: block; }}
  .trend-empty {{
    padding: 60px 20px; text-align: center;
    color: var(--ink-dim); font-style: italic;
    font-size: 16px;
  }}
  .section-title {{
    font-family: 'JetBrains Mono', monospace; font-size: 11px;
    letter-spacing: 0.18em; text-transform: uppercase;
    color: var(--ink-dim);
    margin: 0 0 24px;
    padding-bottom: 12px;
    border-bottom: 1px solid var(--rule);
  }}
  .grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 1px; background: var(--rule); border: 1px solid var(--rule); }}
  .indicator {{
    background: var(--panel);
    padding: 28px;
    display: flex; flex-direction: column;
    min-height: 200px;
    position: relative;
  }}
  .ind-head {{ display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 6px; gap: 12px; }}
  .ind-name-wrap {{ display: flex; align-items: center; gap: 8px; flex: 1; min-width: 0; }}
  .ind-name {{ font-size: 22px; font-weight: 600; letter-spacing: -0.01em; }}
  .info-btn {{
    width: 18px; height: 18px;
    border-radius: 50%;
    border: 1px solid var(--ink-dim);
    background: transparent;
    color: var(--ink-dim);
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px; font-weight: 700;
    font-style: italic;
    cursor: pointer;
    display: inline-flex; align-items: center; justify-content: center;
    padding: 0; line-height: 1;
    transition: all 0.15s ease;
    flex-shrink: 0;
  }}
  .info-btn:hover {{
    border-color: var(--ink);
    color: var(--ink);
  }}
  .info-btn[aria-expanded="true"] {{
    background: var(--ink);
    color: var(--bg);
    border-color: var(--ink);
  }}
  .tooltip {{
    position: absolute;
    top: 60px;
    left: 28px;
    right: 28px;
    background: var(--bg);
    border: 1px solid var(--accent);
    padding: 18px 20px;
    font-size: 14px;
    line-height: 1.55;
    color: var(--ink);
    box-shadow: 0 8px 32px rgba(0,0,0,0.6);
    z-index: 10;
    opacity: 0;
    pointer-events: none;
    transform: translateY(-4px);
    transition: opacity 0.18s ease, transform 0.18s ease;
  }}
  .tooltip.open {{
    opacity: 1;
    pointer-events: auto;
    transform: translateY(0);
  }}
  .tooltip::before {{
    content: "";
    position: absolute;
    top: -7px; left: 18px;
    width: 12px; height: 12px;
    background: var(--bg);
    border-left: 1px solid var(--accent);
    border-top: 1px solid var(--accent);
    transform: rotate(45deg);
  }}
  .tooltip-close {{
    position: absolute;
    top: 8px; right: 12px;
    background: none; border: none; color: var(--ink-dim);
    font-family: 'JetBrains Mono', monospace; font-size: 16px;
    cursor: pointer; padding: 4px;
    line-height: 1;
  }}
  .tooltip-close:hover {{ color: var(--ink); }}
  .ind-score {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 14px; font-weight: 700;
    padding: 4px 10px;
    border: 1px solid currentColor;
  }}
  .ind-note {{ color: var(--ink-dim); font-size: 14px; margin-bottom: 18px; }}
  .ind-source {{ font-family: 'JetBrains Mono', monospace; font-size: 10px; color: var(--ink-dim); letter-spacing: 0.08em; text-transform: uppercase; margin-top: auto; }}
  .bar {{ height: 6px; background: var(--panel-2); position: relative; margin-bottom: 14px; }}
  .bar-fill {{ position: absolute; top: 0; left: 0; bottom: 0; transition: width .6s ease; }}
  .weight {{ font-family: 'JetBrains Mono', monospace; font-size: 10px; color: var(--ink-dim); letter-spacing: 0.08em; }}
  .sparkline {{ width: 100%; height: 40px; margin-bottom: 14px; }}
  footer {{
    margin-top: 64px; padding-top: 24px; border-top: 1px solid var(--rule);
    font-family: 'JetBrains Mono', monospace; font-size: 11px;
    color: var(--ink-dim); letter-spacing: 0.05em;
    display: flex; justify-content: space-between;
  }}
  @media (max-width: 820px) {{
    .hero, .grid {{ grid-template-columns: 1fr; }}
    .score-big {{ font-size: 100px; }}
    h1 {{ font-size: 32px; }}
  }}
</style>
</head>
<body>
<div class="grain"></div>
<div class="wrap">
  <header>
    <h1>Credit Cycle <em>Monitor</em></h1>
    <span class="timestamp">{timestamp}</span>
  </header>

  <section class="hero">
    <div class="score-card">
      <div class="score-label">Composite Stress Score</div>
      <div class="score-big">{score_int}<sup>/100</sup></div>
      <div class="score-band">{band_label}</div>
      <div class="score-action">{band_action}</div>
    </div>
    <div class="legend">
      <h2>Reading the Score</h2>
      <div class="band-row"><span class="band-dot" style="background:#3a7d44"></span><span class="band-range">0—25</span><span class="band-name">Benign <em>— stay invested</em></span></div>
      <div class="band-row"><span class="band-dot" style="background:#c9a227"></span><span class="band-range">25—50</span><span class="band-name">Normal <em>— monitor</em></span></div>
      <div class="band-row"><span class="band-dot" style="background:#d96704"></span><span class="band-range">50—75</span><span class="band-name">Tightening <em>— review risk</em></span></div>
      <div class="band-row"><span class="band-dot" style="background:#b3001b"></span><span class="band-range">75—100</span><span class="band-name">Stressed <em>— go defensive</em></span></div>
    </div>
  </section>

  <h2 class="section-title">Composite Trend</h2>
  <section class="trend-section">
    {trend_chart}
  </section>

  <h2 class="section-title">Component Indicators</h2>
  <div class="grid">
    {indicator_cards}
  </div>

  <footer>
    <span>FRED · Yahoo Finance · Updated {timestamp}</span>
    <span>v1.1</span>
  </footer>
</div>

<script>
  // Tooltip toggling: click the (i) to open, click again or click outside to close
  document.addEventListener('click', function(e) {{
    const btn = e.target.closest('.info-btn');
    const insideTooltip = e.target.closest('.tooltip');
    if (btn) {{
      const id = btn.getAttribute('aria-controls');
      const tooltip = document.getElementById(id);
      const wasOpen = tooltip.classList.contains('open');
      // Close all open tooltips
      document.querySelectorAll('.tooltip.open').forEach(t => t.classList.remove('open'));
      document.querySelectorAll('.info-btn[aria-expanded="true"]').forEach(b => b.setAttribute('aria-expanded', 'false'));
      // Open this one if it was closed
      if (!wasOpen) {{
        tooltip.classList.add('open');
        btn.setAttribute('aria-expanded', 'true');
      }}
    }} else if (e.target.classList.contains('tooltip-close')) {{
      const tooltip = e.target.closest('.tooltip');
      tooltip.classList.remove('open');
      const btn = document.querySelector('.info-btn[aria-controls="' + tooltip.id + '"]');
      if (btn) btn.setAttribute('aria-expanded', 'false');
    }} else if (!insideTooltip) {{
      // Click outside any tooltip — close all
      document.querySelectorAll('.tooltip.open').forEach(t => t.classList.remove('open'));
      document.querySelectorAll('.info-btn[aria-expanded="true"]').forEach(b => b.setAttribute('aria-expanded', 'false'));
    }}
  }});

  // ESC closes tooltips
  document.addEventListener('keydown', function(e) {{
    if (e.key === 'Escape') {{
      document.querySelectorAll('.tooltip.open').forEach(t => t.classList.remove('open'));
      document.querySelectorAll('.info-btn[aria-expanded="true"]').forEach(b => b.setAttribute('aria-expanded', 'false'));
    }}
  }});
</script>
</body>
</html>
"""


def _sparkline_svg(history: list, color: str) -> str:
    """Tiny inline SVG sparkline."""
    if len(history) < 2:
        return ""
    vals = [v for _, v in history]
    lo, hi = min(vals), max(vals)
    rng = hi - lo if hi != lo else 1
    pts = []
    for i, (_, v) in enumerate(history):
        x = (i / (len(history) - 1)) * 100
        y = 100 - ((v - lo) / rng) * 100
        pts.append(f"{x:.1f},{y:.1f}")
    path = "M " + " L ".join(pts)
    return f'''<svg class="sparkline" viewBox="0 0 100 100" preserveAspectRatio="none">
        <path d="{path}" fill="none" stroke="{color}" stroke-width="1.2" vector-effect="non-scaling-stroke"/>
    </svg>'''


def _composite_chart_svg(history: list[tuple[str, float]]) -> str:
    """Larger trend chart for the composite score with band shading."""
    if len(history) < 2:
        return '<div class="trend-empty">Not enough history yet — run daily to build a trend.</div>'

    W, H = 800, 220
    PAD_L, PAD_R, PAD_T, PAD_B = 50, 20, 12, 28

    vals = [v for _, v in history]
    # Always show the full 0-100 range so band shading makes sense
    lo, hi = 0, 100

    def x_for(i):
        return PAD_L + (i / (len(history) - 1)) * (W - PAD_L - PAD_R)

    def y_for(v):
        return PAD_T + (1 - (v - lo) / (hi - lo)) * (H - PAD_T - PAD_B)

    # Band rectangles (Benign, Normal, Tightening, Stressed)
    bands = [
        (0, 25, "#3a7d44"),
        (25, 50, "#c9a227"),
        (50, 75, "#d96704"),
        (75, 100, "#b3001b"),
    ]
    band_rects = []
    for low, high, col in bands:
        y_top = y_for(high)
        y_bot = y_for(low)
        band_rects.append(
            f'<rect x="{PAD_L}" y="{y_top:.1f}" width="{W - PAD_L - PAD_R}" '
            f'height="{y_bot - y_top:.1f}" fill="{col}" opacity="0.08"/>'
        )

    # Y-axis labels at 0/25/50/75/100
    y_labels = []
    for v in [0, 25, 50, 75, 100]:
        y = y_for(v)
        y_labels.append(
            f'<text x="{PAD_L - 8}" y="{y + 3:.1f}" text-anchor="end" '
            f'class="axis-label">{v}</text>'
        )
        y_labels.append(
            f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W - PAD_R}" y2="{y:.1f}" '
            f'stroke="#2e2a25" stroke-width="0.5" stroke-dasharray="2,4"/>'
        )

    # X-axis: show ~5 evenly spaced date labels
    n = len(history)
    label_idxs = [0, n // 4, n // 2, 3 * n // 4, n - 1] if n >= 5 else list(range(n))
    x_labels = []
    for i in label_idxs:
        date_str = history[i][0]
        # Format as "Mon DD" or "MMM 'YY" depending on range
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            if n > 180:
                label = dt.strftime("%b '%y")
            else:
                label = dt.strftime("%b %d")
        except ValueError:
            label = date_str
        x = x_for(i)
        x_labels.append(
            f'<text x="{x:.1f}" y="{H - 8}" text-anchor="middle" class="axis-label">{label}</text>'
        )

    # Main trend line
    pts = [f"{x_for(i):.1f},{y_for(v):.1f}" for i, (_, v) in enumerate(history)]
    path = "M " + " L ".join(pts)

    # Area fill under the line
    area = path + f" L {x_for(n - 1):.1f},{y_for(0):.1f} L {x_for(0):.1f},{y_for(0):.1f} Z"

    # Highlight the latest point
    last_x = x_for(n - 1)
    last_y = y_for(vals[-1])
    last_val = vals[-1]

    return f'''
    <svg viewBox="0 0 {W} {H}" class="trend-chart" preserveAspectRatio="xMidYMid meet">
      <style>
        .axis-label {{ font-family: 'JetBrains Mono', monospace; font-size: 10px; fill: #a09686; letter-spacing: 0.05em; }}
      </style>
      {''.join(band_rects)}
      {''.join(y_labels)}
      {''.join(x_labels)}
      <path d="{area}" fill="url(#trendGrad)" opacity="0.4"/>
      <path d="{path}" fill="none" stroke="#f2eee7" stroke-width="1.8" stroke-linejoin="round"/>
      <circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="4" fill="#f2eee7"/>
      <circle cx="{last_x:.1f}" cy="{last_y:.1f}" r="8" fill="#f2eee7" opacity="0.25"/>
      <text x="{last_x - 8:.1f}" y="{last_y - 10:.1f}" text-anchor="end"
            style="font-family: 'JetBrains Mono', monospace; font-size: 12px; fill: #f2eee7; font-weight: 700;">
        {last_val:.0f}
      </text>
      <defs>
        <linearGradient id="trendGrad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#f2eee7" stop-opacity="0.3"/>
          <stop offset="100%" stop-color="#f2eee7" stop-opacity="0"/>
        </linearGradient>
      </defs>
    </svg>'''


def _score_color(score: float) -> str:
    if score < 25: return "#3a7d44"
    if score < 50: return "#c9a227"
    if score < 75: return "#d96704"
    return "#b3001b"


def render_dashboard(
    indicators: list[IndicatorResult],
    composite: float,
    out_path: Path,
    composite_history: list[tuple[str, float]] | None = None,
):
    band_label, band_color, band_action = stress_band(composite)
    cards = []
    for idx, ind in enumerate(indicators):
        color = _score_color(ind.score)
        spark = _sparkline_svg(ind.history, color)
        tooltip_id = f"tooltip-{idx}"
        # Escape any HTML special chars in explanation (they shouldn't occur but be safe)
        explanation_html = (
            ind.explanation
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        ) if ind.explanation else "No description available."
        info_section = ""
        if ind.explanation:
            info_section = f'''
              <button class="info-btn" aria-controls="{tooltip_id}" aria-expanded="false" aria-label="More info about {ind.name}">i</button>
              <div class="tooltip" id="{tooltip_id}" role="tooltip">
                <button class="tooltip-close" aria-label="Close">×</button>
                {explanation_html}
              </div>
            '''
        cards.append(f"""
        <div class="indicator">
          <div class="ind-head">
            <span class="ind-name-wrap">
              <span class="ind-name">{ind.name}</span>
              {info_section if ind.explanation else ''}
            </span>
            <span class="ind-score" style="color:{color}">{ind.score:.0f}</span>
          </div>
          <div class="ind-note">{ind.note}</div>
          {spark}
          <div class="bar"><div class="bar-fill" style="width:{ind.score:.1f}%;background:{color}"></div></div>
          <div class="ind-source">{ind.source} · weight {ind.weight*100:.0f}%</div>
        </div>
        """)
    trend = _composite_chart_svg(composite_history or [])
    html = HTML_TEMPLATE.format(
        accent=band_color,
        timestamp=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        score_int=int(round(composite)),
        band_label=band_label,
        band_action=band_action,
        trend_chart=trend,
        indicator_cards="\n".join(cards),
    )
    out_path.write_text(html, encoding="utf-8")


# ---------------------------------------------------------------------------
# Email alerting
# ---------------------------------------------------------------------------

def maybe_send_alert(composite: float, indicators: list[IndicatorResult], cfg: dict, state_path: Path):
    email_cfg = cfg.get("email", {})
    if not email_cfg.get("enabled"):
        return

    threshold = float(email_cfg.get("alert_threshold", 50))
    last_band = None
    if state_path.exists():
        try:
            last_band = json.loads(state_path.read_text()).get("band")
        except Exception:
            pass
    current_band, _, action = stress_band(composite)

    # Alert when CROSSING into a higher-stress band (avoids daily spam).
    band_order = ["Benign", "Normal", "Tightening", "Stressed"]
    should_alert = False
    if composite >= threshold:
        if last_band is None or band_order.index(current_band) > band_order.index(last_band):
            should_alert = True

    # Save current state regardless
    state_path.write_text(json.dumps({"band": current_band, "score": composite, "ts": datetime.utcnow().isoformat()}))

    if not should_alert:
        print(f"[INFO] No alert. score={composite:.1f} band={current_band} last={last_band}")
        return

    subject = f"⚠ Credit Stress: {current_band} ({composite:.0f}/100)"
    lines = [
        f"Composite score: {composite:.1f}/100 — {current_band}",
        f"Suggested action: {action}",
        "",
        "Top contributors:",
    ]
    top = sorted(indicators, key=lambda i: i.score * i.weight, reverse=True)[:5]
    for i in top:
        lines.append(f"  • {i.name}: {i.score:.0f}/100  ({i.note})")
    body = "\n".join(lines)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email_cfg["from_addr"]
    msg["To"] = email_cfg["to_addr"]
    msg.set_content(body)

    try:
        with smtplib.SMTP_SSL(email_cfg["smtp_host"], int(email_cfg.get("smtp_port", 465))) as s:
            s.login(email_cfg["smtp_user"], email_cfg["smtp_password"])
            s.send_message(msg)
        print(f"[INFO] Alert email sent to {email_cfg['to_addr']}")
    except Exception as e:
        print(f"[ERROR] Email send failed: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    here = Path(__file__).parent
    cfg_path = here / "config.yaml"
    if not cfg_path.exists():
        print("ERROR: config.yaml not found. Copy config.example.yaml -> config.yaml first.", file=sys.stderr)
        sys.exit(1)
    cfg = yaml.safe_load(cfg_path.read_text())

    print("[INFO] Fetching indicators...")
    indicators = build_indicators(cfg)
    if not indicators:
        print("ERROR: No indicators loaded.", file=sys.stderr)
        sys.exit(2)

    composite = composite_score(indicators)
    band, _, _ = stress_band(composite)
    print(f"[INFO] Composite score: {composite:.1f}/100 ({band})")
    for i in indicators:
        print(f"  - {i.name}: value={i.value:.2f}  score={i.score:.0f}  weight={i.weight}")

    history_path = here / "composite_history.csv"
    composite_history = append_history(composite, history_path)
    print(f"[INFO] History now has {len(composite_history)} entries")

    out = here / "index.html"
    render_dashboard(indicators, composite, out, composite_history=composite_history)
    print(f"[INFO] Dashboard written -> {out}")

    state_path = here / ".state.json"
    maybe_send_alert(composite, indicators, cfg, state_path)


if __name__ == "__main__":
    main()
