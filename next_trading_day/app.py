import json
import math
import os
import re
import time
from datetime import date, datetime, time as clock_time, timedelta
from zoneinfo import ZoneInfo

import boto3
import numpy as np
import pandas as pd
import yfinance as yf
from botocore.config import Config
from ta.momentum import RSIIndicator
from ta.trend import SMAIndicator, MACD

# Preliminary after-close pass. It does not replace before_market and it does
# not write stock_ai_report.json, which after_market evaluates.

# ============================ CONFIGURATION ============================
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
S3_BUCKET = os.getenv("S3_BUCKET", "saravanesh-stock-ai-results")
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "us.amazon.nova-2-lite-v1:0")
# A 1% loss is change_percent <= -1. A positive env value is treated as a loss magnitude.
DROP_THRESHOLD = -abs(float(os.getenv("DROP_THRESHOLD", "-1.0")))
MAX_STOCKS = max(1, int(os.getenv("MAX_STOCKS", "5")))
HISTORY_PERIOD = os.getenv("HISTORY_PERIOD", "5y")
BEDROCK_MAX_TOKENS = int(os.getenv("BEDROCK_MAX_TOKENS", "2000"))
HISTORY_LOOKBACK_DAYS = max(7, int(os.getenv("HISTORY_LOOKBACK_DAYS", "15")))
# Do not screen a partial download as if the rest of the index did not trade.
MIN_SESSION_BARS = max(1, int(os.getenv("MIN_SESSION_BARS", "40")))
REPORT_FILENAME = os.getenv("REPORT_FILENAME", "next_trading_day_report.json")
DATASET_FILENAME = os.getenv("DATASET_FILENAME", "next_trading_day_dataset.json")
LOCAL_TIMEZONE = ZoneInfo("Asia/Kolkata")
# NSE cash market closes 15:30 IST. 15:45 leaves a short publish buffer.
# Target invoke time is 16:00 IST so the report is ready around then.
EARLIEST_RUN = clock_time(15, 45)
RETRY_WAIT_SECONDS = int(os.getenv("RETRY_WAIT_SECONDS", "60"))

# ============================ NIFTY 50 UNIVERSE ============================
NIFTY_50 = [
    "ADANIENT.NS", "ADANIPORTS.NS", "APOLLOHOSP.NS", "ASIANPAINT.NS",
    "AXISBANK.NS", "BAJAJ-AUTO.NS", "BAJFINANCE.NS", "BAJAJFINSV.NS",
    "BEL.NS", "BHARTIARTL.NS", "CIPLA.NS", "COALINDIA.NS", "DRREDDY.NS",
    "EICHERMOT.NS", "ETERNAL.NS", "GRASIM.NS", "HCLTECH.NS", "HDFCBANK.NS",
    "HDFCLIFE.NS", "HEROMOTOCO.NS", "HINDALCO.NS", "HINDUNILVR.NS",
    "ICICIBANK.NS", "INDUSINDBK.NS", "INFY.NS", "ITC.NS", "JIOFIN.NS",
    "JSWSTEEL.NS", "KOTAKBANK.NS", "LT.NS", "M&M.NS", "MARUTI.NS",
    "MAXHEALTH.NS", "NESTLEIND.NS", "NTPC.NS", "ONGC.NS", "POWERGRID.NS",
    "RELIANCE.NS", "SBILIFE.NS", "SBIN.NS", "SHRIRAMFIN.NS", "SUNPHARMA.NS",
    "TATACONSUM.NS", "TATAMOTORS.NS", "TATASTEEL.NS", "TCS.NS", "TECHM.NS",
    "TITAN.NS", "TRENT.NS", "ULTRACEMCO.NS", "WIPRO.NS",
]

# ============================ AWS CLIENTS ============================
AWS_CONFIG = Config(retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=5, read_timeout=30)
s3 = boto3.client("s3", region_name=AWS_REGION, config=AWS_CONFIG)
bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION, config=AWS_CONFIG)

try:
    yf.set_tz_cache_location("/tmp/yfinance")
except Exception as exc:
    print(f"Could not configure yfinance cache location: {exc}")

# ============================ GENERAL HELPERS ============================
def clean_number(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        value = float(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, np.bool_):
        return bool(value)
    return value


def round_or_none(value, places=2):
    value = clean_number(value)
    if value is None:
        return None
    return round(float(value), places)


def safe_float(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def json_dumps(data):
    return json.dumps(data, indent=2, ensure_ascii=False, default=str, allow_nan=False)


def now_ist():
    return datetime.now(LOCAL_TIMEZONE)


def date_prefix(value):
    return value.strftime("%Y/%m/%d")


def index_date(index_value):
    ts = pd.Timestamp(index_value)
    if ts.tzinfo is not None:
        # Yahoo daily bars are exchange-local. Keep that wall-clock date.
        ts = ts.tz_localize(None)
    return ts.date()


def screening_rule(drop_threshold=DROP_THRESHOLD):
    return (
        f"Nifty 50 names down at least {abs(drop_threshold):.2f}% versus the "
        f"previous session close (change_percent <= {drop_threshold:.2f})"
    )


def resolve_session_date(event):
    raw = event.get("session_date") if isinstance(event, dict) else None
    if raw:
        return date.fromisoformat(str(raw))
    return now_ist().date()


def session_ready(now, session_date, allow_open_session=False):
    """Return (ok, reason). Past weekday dates are allowed for a manual backfill."""
    if session_date.weekday() >= 5:
        return False, "Not a weekday. NSE cash market is closed."
    if allow_open_session:
        return True, None
    if session_date > now.date():
        return False, "Session date is in the future."
    if session_date < now.date():
        return True, None
    if now.time() < EARLIEST_RUN:
        return False, (
            "NSE cash session is not closed yet. "
            "Invoke at 16:00 IST, after the 15:30 close."
        )
    return True, None

# ============================ S3 ============================
def upload_json_to_s3(data, s3_key):
    print(f"Uploading to s3://{S3_BUCKET}/{s3_key}")
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=s3_key,
        Body=json_dumps(data).encode("utf-8"),
        ContentType="application/json",
    )
    return f"s3://{S3_BUCKET}/{s3_key}"

# ============================ SCREENING ============================
def select_losers(snapshot, drop_threshold=DROP_THRESHOLD, max_stocks=MAX_STOCKS):
    """Largest decline first. Only names at or below the loss threshold."""
    candidates = [
        item for item in snapshot
        if isinstance(item.get("change_percent_raw"), (int, float))
        and item["change_percent_raw"] <= drop_threshold
    ]
    candidates.sort(key=lambda item: item["change_percent_raw"])
    return candidates[:max_stocks]

# ============================ MARKET DATA ============================
def _closes_by_date(history):
    if history is None or history.empty or "Close" not in history.columns:
        return []
    by_date = {}
    for index_value, row in history.iterrows():
        close = safe_float(row.get("Close"))
        if close is None or close <= 0:
            continue
        volume = safe_float(row.get("Volume")) if "Volume" in history.columns else None
        by_date[index_date(index_value)] = (close, 0 if volume is None else int(volume))
    return sorted(by_date.items())


def session_move(history, session_date):
    """Today's close versus the previous available session, not the previous calendar day."""
    closes = _closes_by_date(history)
    actual = next((item for item in closes if item[0] == session_date), None)
    previous = [item for item in closes if item[0] < session_date]
    if actual is None or not previous:
        return None
    previous_date, (previous_close, _previous_volume) = previous[-1]
    actual_date, (close, volume) = actual
    if previous_close <= 0:
        return None
    change = ((close - previous_close) / previous_close) * 100
    return {
        "session_date": actual_date.isoformat(),
        "previous_session_date": previous_date.isoformat(),
        "previous_close": round(previous_close, 2),
        "close": round(close, 2),
        "change_percent": round(change, 2),
        "change_percent_raw": change,
        "volume": volume,
    }


def download_session_history(ticker, session_date):
    start_date = session_date - timedelta(days=HISTORY_LOOKBACK_DAYS)
    end_date = session_date + timedelta(days=1)
    return yf.Ticker(ticker).history(
        start=start_date.isoformat(),
        end=end_date.isoformat(),
        interval="1d",
        auto_adjust=True,
        actions=False,
    )


def get_session_snapshot(session_date):
    print("\n[1] Downloading today's session closes...")
    snapshot, missing, failed = [], [], []
    for ticker in NIFTY_50:
        try:
            history = download_session_history(ticker, session_date)
            move = session_move(history, session_date)
            if move is None:
                print(f"Skipping {ticker}: no completed bar for {session_date.isoformat()}")
                missing.append(ticker)
                continue
            move["ticker"] = ticker
            snapshot.append(move)
            print(f"{ticker:20} {move['change_percent']:7.2f}%")
        except Exception as exc:
            print(f"ERROR downloading {ticker}: {exc}")
            failed.append(ticker)
    print(
        f"Session scan: {len(snapshot)} with today's bar; "
        f"{len(missing)} missing; {len(failed)} failed"
    )
    return {
        "rows": snapshot,
        "with_bar": len(snapshot),
        "missing_bar": len(missing),
        "failed": len(failed),
        "failed_tickers": failed,
    }

# ============================ HISTORICAL PREVIEW ============================
def summarise_history(history, drop_threshold=DROP_THRESHOLD):
    """Bounce rates after similar one-day losses. Next-session rate is the primary stat."""
    if history is None or getattr(history, "empty", True) or "Close" not in getattr(history, "columns", []):
        return {"error": "No historical data available"}
    history = history.dropna(subset=["Close"]).copy()
    if history.empty:
        return {"error": "No valid closing-price history available"}

    close = history["Close"].astype(float)
    history["Return"] = close.pct_change() * 100
    try:
        history["SMA20"] = SMAIndicator(close, window=20).sma_indicator()
        history["SMA50"] = SMAIndicator(close, window=50).sma_indicator()
        history["RSI"] = RSIIndicator(close, window=14).rsi()
        macd = MACD(close)
        history["MACD"] = macd.macd()
        history["MACD_signal"] = macd.macd_signal()
    except Exception as exc:
        return {"error": f"Technical indicators failed: {exc}"}

    similar = []
    five_day_returns = []
    last_index = len(history) - 1
    for i in range(1, last_index):
        day_return = history["Return"].iloc[i]
        if pd.isna(day_return) or float(day_return) > drop_threshold:
            continue
        start_price = float(history["Close"].iloc[i])
        next_price = float(history["Close"].iloc[i + 1])
        if start_price <= 0 or next_price <= 0 or pd.isna(next_price):
            continue
        next_return = ((next_price - start_price) / start_price) * 100
        similar.append({
            "date": index_date(history.index[i]).isoformat(),
            "drop_percent": round(float(day_return), 2),
            "next_day_return": round(next_return, 2),
        })
        if i + 5 <= last_index:
            future_price = float(history["Close"].iloc[i + 5])
            if future_price > 0 and not pd.isna(future_price):
                five_day_returns.append(((future_price - start_price) / start_price) * 100)

    increase_count = sum(1 for item in similar if item["next_day_return"] > 0)
    if similar:
        increase_rate = 100 * increase_count / len(similar)
        average_next_day = float(np.mean([item["next_day_return"] for item in similar]))
        historical_bias = "INCREASE" if increase_rate >= 50 else "DECREASE"
    else:
        increase_rate = average_next_day = None
        historical_bias = "UNKNOWN"

    if five_day_returns:
        five_day_rate = 100 * sum(value > 0 for value in five_day_returns) / len(five_day_returns)
        average_five_day = float(np.mean(five_day_returns))
    else:
        five_day_rate = average_five_day = None

    latest = history.iloc[-1]
    last_close = clean_number(latest.get("Close"))
    sma20 = clean_number(latest.get("SMA20"))
    close_vs_sma20 = None
    if last_close is not None and sma20 not in (None, 0):
        close_vs_sma20 = ((last_close - sma20) / sma20) * 100

    return {
        "latest": {
            "date": index_date(history.index[-1]).isoformat(),
            "close": round_or_none(last_close),
            "rsi": round_or_none(latest.get("RSI")),
            "sma20": round_or_none(sma20),
            "sma50": round_or_none(latest.get("SMA50")),
            "macd": round_or_none(latest.get("MACD")),
            "macd_signal": round_or_none(latest.get("MACD_signal")),
            "close_vs_sma20_percent": round_or_none(close_vs_sma20),
        },
        "historical_statistics": {
            "similar_drop_events": len(similar),
            "historical_next_day_increase_count": increase_count,
            "historical_next_day_increase_rate_percent": round_or_none(increase_rate),
            "average_next_day_return_percent": round_or_none(average_next_day),
            "historical_five_day_recovery_rate_percent": round_or_none(five_day_rate),
            "average_five_day_return_percent": round_or_none(average_five_day),
            "historical_bias": historical_bias,
        },
        "recent_similar_drops": similar[-8:],
    }


def get_historical_preview(ticker):
    print(f"\n[3] Historical preview: {ticker}")
    try:
        history = yf.Ticker(ticker).history(
            period=HISTORY_PERIOD, interval="1d", auto_adjust=True, actions=False
        )
    except Exception as exc:
        print(f"Historical download failed for {ticker}: {exc}")
        return {"error": f"Historical data download failed: {exc}"}
    return summarise_history(history)

# ============================ COMPANY NAME ============================
def get_company_name(ticker):
    try:
        info = yf.Ticker(ticker).info
        name = info.get("longName") or info.get("shortName")
        if name:
            return str(name)
    except Exception as exc:
        print(f"Company name lookup failed for {ticker}: {exc}")
    return ticker.removesuffix(".NS")

# ============================ BEDROCK ============================
def parse_model_json(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def outlook_from_model(parsed):
    """Yes/no increase call is derived from probability so it cannot contradict it."""
    if not isinstance(parsed, dict):
        raise ValueError("Bedrock response was not a JSON object")
    probability = safe_float(parsed.get("probability_positive_percent"))
    if probability is None or not 0 <= probability <= 100:
        raise ValueError("Missing or invalid probability_positive_percent")
    will_increase = probability >= 50.0
    low = safe_float(parsed.get("expected_range_low_percent"))
    high = safe_float(parsed.get("expected_range_high_percent"))
    if low is None or high is None or low > high:
        low = high = None
    reason = str(parsed.get("reason") or "").strip() or None
    key_risk = str(parsed.get("key_risk") or "").strip() or None
    return {
        "will_increase": will_increase,
        "predicted_direction": "POSITIVE" if will_increase else "NEGATIVE",
        "probability_positive_percent": round(probability, 2),
        "confidence": "HIGH" if probability >= 60 or probability <= 40 else "LOW",
        "verdict": "INCREASE" if will_increase else "DECREASE",
        "expected_next_day_move_percent": round_or_none(parsed.get("expected_next_day_move_percent")),
        "expected_range_low_percent": round_or_none(low),
        "expected_range_high_percent": round_or_none(high),
        "reason": reason,
        "key_risk": key_risk,
    }


def analyze_with_bedrock(record):
    ticker = record["ticker"]
    print(f"\n[4] Preliminary Bedrock outlook: {ticker}")
    system_prompt = """
You are an equity research analysis agent, not a market oracle. Use only supplied evidence;
do not invent news, prices, financial figures, or facts. This is a preliminary after-close
check, not a full research note. Estimate whether the next NSE session close will be above
today's close. Be conservative; a one-day drop does not mean the stock must bounce.
Return only valid JSON.
"""
    user_prompt = f"""
Today's NSE session is closed. Using ONLY the supplied data, estimate whether the next
session close is above today's close.

{json_dumps(record)}

Return a JSON object with exactly these fields:
{{
  "ticker": "...",
  "company": "...",
  "probability_positive_percent": 0,
  "expected_next_day_move_percent": 0,
  "expected_range_low_percent": 0,
  "expected_range_high_percent": 0,
  "reason": "...",
  "key_risk": "..."
}}
probability_positive_percent is the chance the next session close is above today's close, from 0 to 100.
expected_next_day_move_percent is the estimated percentage move from today's close.
expected_range_low_percent must be less than or equal to expected_range_high_percent.
Do not give a probability of 90 or above unless the evidence is exceptionally strong.
This is experimental research, not financial advice.
"""
    response = bedrock.converse(
        modelId=BEDROCK_MODEL_ID,
        system=[{"text": system_prompt}],
        messages=[{"role": "user", "content": [{"text": user_prompt}]}],
        inferenceConfig={"maxTokens": BEDROCK_MAX_TOKENS, "temperature": 0.1},
    )
    content = response["output"]["message"]["content"]
    text = "".join(block.get("text", "") for block in content if "text" in block)
    return outlook_from_model(parse_model_json(text))

# ============================ REPORT ============================
def build_candidate(snapshot_item, company, historical, outlook, status, error=None):
    stats = (historical or {}).get("historical_statistics") or {}
    latest = (historical or {}).get("latest") or {}
    row = {
        "ticker": snapshot_item["ticker"],
        "company": company,
        "status": status,
        "drop_rank": snapshot_item.get("drop_rank"),
        "session_date": snapshot_item.get("session_date"),
        "previous_session_date": snapshot_item.get("previous_session_date"),
        "previous_close": snapshot_item.get("previous_close"),
        "close": snapshot_item.get("close"),
        "change_percent": snapshot_item.get("change_percent"),
        "volume": snapshot_item.get("volume"),
        "rsi": latest.get("rsi"),
        "sma20": latest.get("sma20"),
        "sma50": latest.get("sma50"),
        "close_vs_sma20_percent": latest.get("close_vs_sma20_percent"),
        "macd": latest.get("macd"),
        "macd_signal": latest.get("macd_signal"),
        "similar_drop_events": stats.get("similar_drop_events"),
        "historical_next_day_increase_count": stats.get("historical_next_day_increase_count"),
        "historical_next_day_increase_rate_percent": stats.get("historical_next_day_increase_rate_percent"),
        "average_next_day_return_percent": stats.get("average_next_day_return_percent"),
        "historical_five_day_recovery_rate_percent": stats.get("historical_five_day_recovery_rate_percent"),
        "average_five_day_return_percent": stats.get("average_five_day_return_percent"),
        "historical_bias": stats.get("historical_bias"),
    }
    if outlook:
        row.update(outlook)
    if error:
        row["error"] = error
    if historical and historical.get("error"):
        row["history_error"] = historical["error"]
    return row


def build_report(session_date, scan, eligible_count, candidates):
    with_outlook = sum(1 for item in candidates if item.get("status") == "SUCCESS")
    return {
        "generated_at": now_ist().isoformat(),
        "timezone": "Asia/Kolkata",
        "run_type": "next_trading_day",
        "title": "Next Trading Day",
        "preliminary": True,
        "session_date": session_date.isoformat(),
        "outlook_for": "Next NSE session close versus today's close",
        "description": (
            "Preliminary after-close outlook for Nifty 50 names that fell at least "
            "1% today. Not the morning research report, and not financial advice."
        ),
        "screening_rule": screening_rule(),
        "schedule": "16:00 IST, weekdays, after the NSE cash close",
        "model": BEDROCK_MODEL_ID,
        "warning": "Experimental research model. Not financial advice.",
        "stocks_in_universe": len(NIFTY_50),
        "stocks_with_session_bar": scan["with_bar"],
        "stocks_missing_session_bar": scan["missing_bar"],
        "stocks_download_failed": scan["failed"],
        "stocks_eligible": eligible_count,
        "stocks_selected": len(candidates),
        "stocks_requested": MAX_STOCKS,
        "stocks_with_outlook": with_outlook,
        "stocks_partial": sum(1 for item in candidates if item.get("status") == "PARTIAL"),
        "stocks_failed": sum(1 for item in candidates if item.get("status") == "FAILED"),
        "candidates": candidates,
    }

# ============================ MAIN APPLICATION ============================
def _skip(session_date, reason, scan=None):
    print(f"SKIPPED {session_date.isoformat()}: {reason}")
    result = {
        "status": "SKIPPED",
        "session_date": session_date.isoformat(),
        "reason": reason,
        "timezone": "Asia/Kolkata",
    }
    if scan:
        result.update({
            "stocks_with_session_bar": scan["with_bar"],
            "stocks_missing_session_bar": scan["missing_bar"],
            "stocks_download_failed": scan["failed"],
        })
    return result


def _scan_until_complete(session_date):
    scan = get_session_snapshot(session_date)
    if scan["with_bar"] >= MIN_SESSION_BARS or RETRY_WAIT_SECONDS <= 0:
        return scan
    print(
        f"Only {scan['with_bar']} session bars (need {MIN_SESSION_BARS}). "
        f"Waiting {RETRY_WAIT_SECONDS}s for today's close to publish..."
    )
    time.sleep(RETRY_WAIT_SECONDS)
    return get_session_snapshot(session_date)


def analyse_candidate(candidate):
    ticker = candidate["ticker"]
    company = get_company_name(ticker)
    print("\n" + "=" * 38 + f"\nPreliminary outlook {company} ({ticker})\n" + "=" * 38)
    historical = get_historical_preview(ticker)
    model_input = {
        "analysis_timestamp": now_ist().isoformat(),
        "ticker": ticker,
        "company": company,
        "session": {key: value for key, value in candidate.items() if key != "change_percent_raw"},
        "historical": historical,
        "question": "Will the next NSE session close be above today's close?",
    }
    try:
        outlook = analyze_with_bedrock(model_input)
        row = build_candidate(candidate, company, historical, outlook, "SUCCESS")
        return model_input, row
    except Exception as exc:
        print(f"Outlook failed for {ticker}: {exc}")
        row = build_candidate(candidate, company, historical, None, "PARTIAL", error=str(exc))
        return model_input, row


def main(event=None):
    event = event if isinstance(event, dict) else {}
    session_date = resolve_session_date(event)
    allow_open = event.get("allow_open_session") is True
    print("\n" + "=" * 46 + "\n    NEXT TRADING DAY PRELIMINARY SCAN\n" + "=" * 46)
    print(f"India time: {now_ist().isoformat()}")
    print(f"Session date: {session_date.isoformat()}")
    print(f"Drop threshold: {DROP_THRESHOLD:.2f}%")
    print(f"Maximum candidates: {MAX_STOCKS}")

    ready, reason = session_ready(now_ist(), session_date, allow_open_session=allow_open)
    if not ready:
        return _skip(session_date, reason)

    scan = _scan_until_complete(session_date)
    if scan["with_bar"] < MIN_SESSION_BARS:
        return _skip(
            session_date,
            (
                f"No completed NSE session for {session_date.isoformat()} "
                f"({scan['with_bar']} of {len(NIFTY_50)} closes found; "
                f"need {MIN_SESSION_BARS}). Holiday, or today's close is not published yet."
            ),
            scan,
        )

    eligible = select_losers(scan["rows"], max_stocks=len(scan["rows"]))
    selected = eligible[:MAX_STOCKS]
    for rank, item in enumerate(selected, start=1):
        item["drop_rank"] = rank
    print(f"Eligible: {len(eligible)}; selected: {len(selected)}")
    prefix = date_prefix(session_date)

    if not selected:
        report = build_report(session_date, scan, 0, [])
        report["message"] = "Session scan completed; no names met the loss threshold."
        location = upload_json_to_s3(report, f"{prefix}/{REPORT_FILENAME}")
        print(f"No eligible names. Report saved to {location}")
        return {
            "status": "COMPLETED",
            "message": report["message"],
            "session_date": session_date.isoformat(),
            "report_location": location,
            "stocks_eligible": 0,
            "stocks_selected": 0,
            "stocks_with_outlook": 0,
        }

    dataset, candidates = [], []
    for candidate in selected:
        try:
            model_input, row = analyse_candidate(candidate)
            dataset.append(model_input)
            candidates.append(row)
        except Exception as exc:
            print(f"Failed to analyse {candidate['ticker']}: {exc}")
            candidates.append(build_candidate(
                candidate, candidate["ticker"].removesuffix(".NS"),
                None, None, "FAILED", error=str(exc),
            ))
        time.sleep(0.5)

    dataset_key = f"{prefix}/{DATASET_FILENAME}"
    report_key = f"{prefix}/{REPORT_FILENAME}"
    dataset_location = upload_json_to_s3(dataset, dataset_key)
    report = build_report(session_date, scan, len(eligible), candidates)
    report_location = upload_json_to_s3(report, report_key)

    print("\n" + "=" * 46 + "\n       NEXT TRADING DAY OUTLOOK\n" + "=" * 46)
    for row in candidates:
        print(
            f"{row.get('drop_rank')}. {row.get('ticker')} {row.get('change_percent')}% | "
            f"{row.get('status')} | increase={row.get('will_increase')} | "
            f"p={row.get('probability_positive_percent')} | {row.get('verdict')}"
        )
    print(f"Dataset: {dataset_location}\nReport: {report_location}")
    return {
        "status": "COMPLETED",
        "message": "Next trading day preliminary outlook completed",
        "s3_bucket": S3_BUCKET,
        "session_date": session_date.isoformat(),
        "dataset_key": dataset_key,
        "report_key": report_key,
        "dataset_location": dataset_location,
        "report_location": report_location,
        "stocks_eligible": len(eligible),
        "stocks_selected": len(candidates),
        "stocks_with_outlook": report["stocks_with_outlook"],
    }

# ============================ AWS LAMBDA HANDLER ============================
def lambda_handler(event, context):
    request_id = getattr(context, "aws_request_id", "local-run") if context else "local-run"
    print("=" * 46 + "\nNext Trading Day Lambda started")
    print(f"Request ID: {request_id}\n" + "=" * 46)
    try:
        result = main(event if isinstance(event, dict) else {})
        return {"statusCode": 200, "body": json.dumps(result, default=str)}
    except Exception as exc:
        print(f"FATAL ERROR: {exc}")
        return {
            "statusCode": 500,
            "body": json.dumps({
                "status": "FAILED",
                "error": str(exc),
            }),
        }


if __name__ == "__main__":
    print(json.dumps(lambda_handler({}, None), indent=2))
