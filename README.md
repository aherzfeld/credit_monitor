# Credit Cycle Monitor

A small Python tool that tracks the U.S. credit cycle by pulling indicators
from FRED and Yahoo Finance, scoring each on a 0–100 stress scale, and
combining them into a weighted composite score. Renders an HTML dashboard
with trend chart and explanatory tooltips, and emails you when stress
crosses thresholds.

## What it tracks

| Indicator | Weight | Source |
|---|---|---|
| High-Yield OAS | 25% | FRED `BAMLH0A0HYM2` |
| Investment-Grade OAS | 10% | FRED `BAMLC0A0CM` |
| 2s10s Yield Curve | 15% | FRED `T10Y2Y` |
| SLOOS C&I Tightening | 15% | FRED `DRTSCILM` (quarterly) |
| Credit Card Delinquency | 10% | FRED `DRCCLACBS` |
| KRE/SPY 3-Month | 10% | Yahoo |
| EM Debt (EMB) 6-Month | 10% | Yahoo |
| HYG/LQD 3-Month | 5% | Yahoo |

Click the **(i)** button on any indicator card in the dashboard for a
plain-English explanation of what it measures and why it matters.

## Score bands

- **0–25 Benign** — credit calm, stay invested
- **25–50 Normal** — monitor weekly
- **50–75 Tightening** — review cyclical exposure
- **75–100 Stressed** — defensive positioning warranted

The composite score is logged to `composite_history.csv` on every run so
the dashboard builds up a trend chart over time.

## Setup

### 1. Get a FRED API key

Free, 30 seconds: https://fred.stlouisfed.org/docs/api/api_key.html

### 2. Set up an "app password" for email (if using Gmail)

Gmail won't let you SMTP in with your real password — you need an App Password:

1. Enable 2-factor auth on your Google account
2. Go to https://myaccount.google.com/apppasswords
3. Generate a 16-character app password — use this in the config

### 3. Install and configure

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml
# edit config.yaml with your FRED key and email settings
```

### 4. Run it

```bash
python credit_monitor.py
```

This writes `dashboard.html` (open in a browser), appends today's score to
`composite_history.csv`, and emails you if the score crossed into a higher
stress band since the last run.

## Automating it (recommended: GitHub Actions, free)

The included `.github/workflows/daily.yml` runs the script every weekday
at 22:00 UTC, commits the updated dashboard + history back to the repo,
and emails alerts. To use it:

1. Push this folder to a **private** GitHub repo
2. Repo Settings → Secrets and variables → Actions, add:
   - `FRED_API_KEY`
   - `SMTP_USER` (your Gmail address)
   - `SMTP_PASSWORD` (the 16-char app password)
   - `ALERT_TO_ADDR` (where alerts go — can be the same as SMTP_USER)
3. Done. Check the Actions tab to confirm it runs.

You can also enable GitHub Pages on the repo, pointed at the root, and the
dashboard becomes a live URL you can bookmark on your phone.

## Alternative scheduling

- **macOS/Linux cron**: `0 22 * * 1-5 cd /path/to/credit_monitor && python credit_monitor.py`
- **Windows Task Scheduler**: create a daily task that runs `python credit_monitor.py`

## Tuning

The thresholds in `credit_monitor.py` are calibrated to historical norms but
opinionated. To adjust:

- Change `weight` values in `build_indicators()` to re-weight inputs
- Change the `benign` / `stressed` arguments in each `score_linear()` call
  to shift what counts as elevated for that metric
- Change `alert_threshold` in `config.yaml` to make alerts more/less sensitive
- Tooltip explanations live in the `EXPLANATIONS` dict near the top of
  `credit_monitor.py` — edit those strings to customize

## Files

- `credit_monitor.py` — main script
- `config.example.yaml` — copy to `config.yaml` and edit (gitignored)
- `requirements.txt` — Python deps
- `dashboard.html` — generated output (committed by Actions)
- `composite_history.csv` — daily score log (committed by Actions)
- `.state.json` — tracks last alert band (gitignored)
- `.github/workflows/daily.yml` — GitHub Actions schedule
- `.gitignore` — keeps secrets and local state out of the repo
