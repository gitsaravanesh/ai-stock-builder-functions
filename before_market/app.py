import json
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

import boto3
import feedparser
import numpy as np
import pandas as pd
import requests
import yfinance as yf
from botocore.config import Config
from ta.momentum import RSIIndicator
from ta.trend import SMAIndicator, MACD

# Lambda's filesystem is read-only except /tmp; the yfinance cache cannot
# write under /home/sbx_user1051.
try:
    yf.set_tz_cache_location("/tmp/yfinance")
except Exception as exc:
    print(f"Could not configure yfinance cache location: {exc}")

# ============================ CONFIGURATION ============================
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
S3_BUCKET = os.getenv("S3_BUCKET", "saravanesh-stock-ai-results")
BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID", "us.amazon.nova-2-lite-v1:0")
# A 1% decline means change_percent <= -1.00. Override via Lambda env var if needed.
DROP_THRESHOLD = float(os.getenv("DROP_THRESHOLD", "-1.0"))
MAX_STOCKS = max(1, int(os.getenv("MAX_STOCKS", "5")))
HISTORY_PERIOD = os.getenv("HISTORY_PERIOD", "5y")
MAX_NEWS = max(1, int(os.getenv("MAX_NEWS", "10")))
HTTP_TIMEOUT_SECONDS = int(os.getenv("HTTP_TIMEOUT_SECONDS", "15"))
BEDROCK_MAX_TOKENS = int(os.getenv("BEDROCK_MAX_TOKENS", "4000"))
LOCAL_TIMEZONE = ZoneInfo("Asia/Kolkata")
GOOGLE_NEWS_URL = "https://news.google.com/rss/search?q={query}&hl=en-IN&gl=IN&ceid=IN:en"

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
    "TITAN.NS", "TRENT.NS", "ULTRACEMCO.NS", "WIPRO.NS"
]

# ============================ AWS CLIENTS ============================
AWS_CONFIG = Config(retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=5, read_timeout=30)
s3 = boto3.client("s3", region_name=AWS_REGION, config=AWS_CONFIG)
bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION, config=AWS_CONFIG)

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
    if isinstance(value, (float,)):
        return value if math.isfinite(value) else None
    if isinstance(value, np.bool_):
        return bool(value)
    return value


def dataframe_to_records(df):
    if df is None or df.empty:
        return []
    records = []
    for index, row in df.iterrows():
        date_value = index.date().isoformat() if hasattr(index, "date") else str(index)
        record = {"date": date_value}
        for column in df.columns:
            record[str(column)] = clean_number(row[column])
        records.append(record)
    return records


def get_india_datetime():
    return datetime.now(LOCAL_TIMEZONE)


def get_s3_prefix():
    now = get_india_datetime()
    return f"{now.year:04d}/{now.month:02d}/{now.day:02d}"


def json_dumps(data):
    return json.dumps(data, indent=2, ensure_ascii=False, default=str, allow_nan=False)

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

# ============================ MARKET DATA ============================
def get_market_snapshot():
    """Compare the two latest daily bars available from Yahoo Finance."""
    print("\n[1] Downloading market data...")
    snapshot, failed = [], []
    for ticker in NIFTY_50:
        try:
            history = yf.Ticker(ticker).history(period="10d", interval="1d", auto_adjust=True, actions=False)
            if history is None or history.empty:
                print(f"Skipping {ticker}: no data")
                failed.append(ticker)
                continue
            history = history.dropna(subset=["Close"])
            if len(history) < 2:
                print(f"Skipping {ticker}: fewer than two valid closes")
                failed.append(ticker)
                continue
            previous_close = float(history["Close"].iloc[-2])
            current_close = float(history["Close"].iloc[-1])
            if previous_close <= 0:
                print(f"Skipping {ticker}: invalid previous close")
                failed.append(ticker)
                continue
            change = ((current_close - previous_close) / previous_close) * 100
            volume_value = history["Volume"].iloc[-1] if "Volume" in history else 0
            latest_index = history.index[-1]
            market_date = latest_index.date().isoformat() if hasattr(latest_index, "date") else str(latest_index)
            item = {
                "ticker": ticker,
                "market_data_date": market_date,
                "previous_close": round(previous_close, 2),
                "current_close": round(current_close, 2),
                "change_percent": round(change, 2),
                "change_percent_raw": change,
                "volume": int(volume_value) if pd.notna(volume_value) else 0,
            }
            snapshot.append(item)
            print(f"{ticker:20} {change:7.2f}% ({market_date})")
        except Exception as exc:
            print(f"ERROR downloading {ticker}: {exc}")
            failed.append(ticker)
    print(f"Market scan complete: {len(snapshot)} valid; {len(failed)} failed/skipped")
    if failed:
        print("Failed/skipped: " + ", ".join(failed))
    return snapshot

# ============================ SCREENING ============================
def select_candidates(snapshot):
    print("\n[2] Screening stocks...")
    candidates = [
        item for item in snapshot
        if isinstance(item.get("change_percent"), (int, float))
        and item.get("change_percent_raw", item["change_percent"]) <= DROP_THRESHOLD
    ]
    candidates.sort(key=lambda item: item.get("change_percent_raw", item["change_percent"]))  # largest decline first
    selected = candidates[:MAX_STOCKS]
    print(f"Threshold: <= {DROP_THRESHOLD:.2f}%; eligible: {len(candidates)}; selected: {len(selected)}")
    for item in selected:
        print(f"{item['ticker']:20} {item['change_percent']:7.2f}%")
    return selected

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

# ============================ HISTORICAL ANALYSIS ============================
def get_historical_analysis(ticker):
    print(f"\n[3] Historical analysis: {ticker}")
    try:
        history = yf.Ticker(ticker).history(period=HISTORY_PERIOD, interval="1d", auto_adjust=True, actions=False)
    except Exception as exc:
        print(f"Historical download failed for {ticker}: {exc}")
        return {"error": f"Historical data download failed: {exc}"}
    if history is None or history.empty:
        return {"error": "No historical data available"}
    history = history.dropna(subset=["Close"]).copy()
    if history.empty:
        return {"error": "No valid closing-price history available"}

    history["Return"] = history["Close"].pct_change() * 100
    history["SMA20"] = SMAIndicator(history["Close"], window=20).sma_indicator()
    history["SMA50"] = SMAIndicator(history["Close"], window=50).sma_indicator()
    history["RSI"] = RSIIndicator(history["Close"], window=14).rsi()
    macd = MACD(history["Close"])
    history["MACD"] = macd.macd()
    history["MACD_signal"] = macd.macd_signal()

    # Use the same -1% threshold for historical analogues. Require five future
    # sessions so every event has comparable 1-, 3-, and 5-session returns.
    similar_events = []
    for i in range(max(0, len(history) - 5)):
        day_return = history["Return"].iloc[i]
        if pd.isna(day_return) or float(day_return) > DROP_THRESHOLD:
            continue
        start_price = float(history["Close"].iloc[i])
        if start_price <= 0:
            continue
        future = {}
        for horizon in (1, 3, 5):
            price = float(history["Close"].iloc[i + horizon])
            future[horizon] = ((price - start_price) / start_price) * 100
        similar_events.append({
            "date": history.index[i].date().isoformat(),
            "drop_percent": round(float(day_return), 2),
            "next_day_return": round(future[1], 2),
            "three_day_return": round(future[3], 2),
            "five_day_return": round(future[5], 2),
        })

    if similar_events:
        recovery_rate = 100 * sum(x["five_day_return"] > 0 for x in similar_events) / len(similar_events)
        average_next_day = float(np.mean([x["next_day_return"] for x in similar_events]))
        average_five_day = float(np.mean([x["five_day_return"] for x in similar_events]))
    else:
        recovery_rate = average_next_day = average_five_day = None

    latest = history.iloc[-1]
    columns = ["Open", "High", "Low", "Close", "Volume", "Return", "SMA20", "SMA50", "RSI", "MACD", "MACD_signal"]
    return {
        "latest": {
            "date": history.index[-1].date().isoformat(),
            "close": clean_number(latest.get("Close")),
            "volume": clean_number(latest.get("Volume")),
            "rsi": clean_number(latest.get("RSI")),
            "sma20": clean_number(latest.get("SMA20")),
            "sma50": clean_number(latest.get("SMA50")),
            "macd": clean_number(latest.get("MACD")),
            "macd_signal": clean_number(latest.get("MACD_signal")),
        },
        "historical_statistics": {
            "similar_drop_events": len(similar_events),
            "historical_recovery_rate_percent": clean_number(recovery_rate),
            "average_next_day_return_percent": clean_number(average_next_day),
            "average_five_day_return_percent": clean_number(average_five_day),
        },
        "similar_drop_events": similar_events[-20:],
        "recent_history": dataframe_to_records(history.tail(30)[columns]),
    }

# ============================ NEWS ============================
def get_news(ticker, company_name):
    print(f"\n[4] News search: {company_name}")
    url = GOOGLE_NEWS_URL.format(query=quote(f'"{company_name}" stock OR shares'))
    try:
        response = requests.get(
            url, timeout=HTTP_TIMEOUT_SECONDS,
            headers={"User-Agent": "Mozilla/5.0 StockAIResearch/1.0"},
        )
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        articles = []
        for entry in feed.entries:
            if len(articles) >= MAX_NEWS:
                break
            published = entry.get("published_parsed")
            published_iso = None
            if published:
                published_dt = datetime(*published[:6], tzinfo=timezone.utc)
                if published_dt < cutoff:
                    continue
                published_iso = published_dt.isoformat()
            source = entry.get("source")
            articles.append({
                "title": entry.get("title"),
                "summary": entry.get("summary"),
                "url": entry.get("link"),
                "published": published_iso,
                "source": source.get("title") if isinstance(source, dict) else None,
            })
        print(f"Found {len(articles)} recent news articles")
        return articles
    except Exception as exc:
        print(f"News error for {ticker}: {exc}")
        return []

# ============================ FUNDAMENTALS ============================
def get_fundamentals(ticker):
    print(f"\n[5] Fundamentals: {ticker}")
    fields = [
        "longName", "sector", "industry", "marketCap", "trailingPE", "forwardPE",
        "priceToBook", "returnOnEquity", "profitMargins", "operatingMargins",
        "revenueGrowth", "earningsGrowth", "debtToEquity", "currentRatio",
        "freeCashflow", "operatingCashflow", "totalRevenue", "totalDebt", "totalCash",
        "recommendationKey", "numberOfAnalystOpinions",
    ]
    try:
        info = yf.Ticker(ticker).info
        return {field: clean_number(info.get(field)) for field in fields}
    except Exception as exc:
        print(f"Fundamental error for {ticker}: {exc}")
        return {"error": str(exc)}

# ============================ BUILD STOCK DATASET ============================
def build_stock_record(candidate):
    ticker = candidate["ticker"]
    company = get_company_name(ticker)
    print("\n" + "=" * 38 + f"\nAnalyzing {company} ({ticker})\n" + "=" * 38)
    return {
        "analysis_timestamp": get_india_datetime().isoformat(),
        "ticker": ticker,
        "company": company,
        "market_snapshot": candidate,
        "historical": get_historical_analysis(ticker),
        "news": get_news(ticker, company),
        "fundamentals": get_fundamentals(ticker),
    }

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


def analyze_with_bedrock(stock_record):
    ticker = stock_record["ticker"]
    print(f"\n[6] Sending {ticker} to Amazon Bedrock...")
    system_prompt = """
You are an equity research analysis agent, not a market oracle. Use only supplied evidence;
do not invent news, prices, financial figures, or facts. Estimate the probability that the next
trading day's close is above today's close, the expected next-day percentage move, and setup
quality/risk. Consider recent price movement, historical behaviour after similar declines,
technicals, volume, news, fundamentals, catalysts and risks. Be conservative; oversold alone
does not mean a stock must recover. Return only valid JSON.
"""
    user_prompt = f"""
Analyze this stock using ONLY the supplied data:
{json_dumps(stock_record)}
Return a JSON object with exactly these fields:
{{
  "ticker":"...", "company":"...", "overall_score":0,
  "next_day_probability_positive_percent":0, "expected_next_day_move_percent":0,
  "expected_range_low_percent":0, "expected_range_high_percent":0,
  "news_score":0, "technical_score":0, "historical_score":0,
  "fundamental_score":0, "risk_score":0, "news_assessment":"...",
  "historical_assessment":"...", "technical_assessment":"...",
  "fundamental_assessment":"...", "bullish_factors":[], "bearish_factors":[],
  "key_catalyst":"...", "key_risk":"...",
  "verdict":"STRONG_CANDIDATE | CANDIDATE | WATCH | AVOID", "reasoning":"..."
}}
overall_score: 0=extremely bearish, 50=neutral, 100=extremely bullish.
Probability means chance next trading day's close is above today's close. Expected move is the
estimated percentage move from today's close. risk_score: 0=lower risk, 100=very high risk.
Do not give probability 90%+ unless evidence is exceptionally strong. This is experimental
research, not financial advice.
"""
    try:
        response = bedrock.converse(
            modelId=BEDROCK_MODEL_ID,
            system=[{"text": system_prompt}],
            messages=[{"role": "user", "content": [{"text": user_prompt}]}],
            inferenceConfig={"maxTokens": BEDROCK_MAX_TOKENS, "temperature": 0.1},
        )
        content = response["output"]["message"]["content"]
        text = "".join(block.get("text", "") for block in content if "text" in block)
        result = parse_model_json(text)
        if not isinstance(result, dict):
            raise ValueError("Bedrock response was not a JSON object")
        result.setdefault("ticker", ticker)
        result.setdefault("company", stock_record.get("company", ticker))
        return result
    except Exception as exc:
        print(f"Bedrock error for {ticker}: {exc}")
        return {"ticker": ticker, "company": stock_record.get("company", ticker), "error": str(exc)}

# ============================ FINAL REPORT ============================
def create_final_report(results, market_snapshot, candidates):
    valid = [item for item in results if isinstance(item, dict) and "error" not in item]
    valid.sort(key=lambda item: float(item.get("overall_score") or 0), reverse=True)
    return {
        "generated_at": get_india_datetime().isoformat(),
        "timezone": "Asia/Kolkata",
        "description": "AI-generated stock recovery analysis",
        "screening_rule": f"Stocks with daily decline <= {DROP_THRESHOLD:.2f}%",
        "stocks_scanned": len(market_snapshot),
        "stocks_eligible": len(candidates),
        "stocks_selected": len(valid),
        "stocks_requested": MAX_STOCKS,
        "model": BEDROCK_MODEL_ID,
        "warning": "Experimental research model. Not financial advice.",
        "ranked_candidates": valid,
    }

# ============================ MAIN APPLICATION ============================
def main():
    print("\n" + "=" * 46 + "\n       AI STOCK RECOVERY AGENT\n" + "=" * 46)
    print(f"India time: {get_india_datetime().isoformat()}")
    print(f"Drop threshold: {DROP_THRESHOLD:.2f}%")
    print(f"Maximum candidates: {MAX_STOCKS}")
    print(f"Bedrock model: {BEDROCK_MODEL_ID}")
    print(f"S3 bucket: {S3_BUCKET}")

    snapshot = get_market_snapshot()
    # An empty snapshot means the data provider failed; don't disguise it as
    # a successful run with zero eligible stocks.
    if not snapshot:
        raise RuntimeError("Market scan returned no valid stock data; check Yahoo Finance/network logs")

    candidates = select_candidates(snapshot)
    prefix = get_s3_prefix()

    if not candidates:
        report = {
            "generated_at": get_india_datetime().isoformat(),
            "timezone": "Asia/Kolkata",
            "description": "AI-generated stock recovery analysis",
            "screening_rule": f"Stocks with daily decline <= {DROP_THRESHOLD:.2f}%",
            "stocks_scanned": len(snapshot),
            "stocks_eligible": 0,
            "stocks_selected": 0,
            "stocks_requested": MAX_STOCKS,
            "model": BEDROCK_MODEL_ID,
            "warning": "Experimental research model. Not financial advice.",
            "ranked_candidates": [],
            "message": "Market scan completed; no stocks met the decline threshold.",
        }
        location = upload_json_to_s3(report, f"{prefix}/stock_ai_report.json")
        print(f"No eligible stocks. Report saved to {location}")
        return report

    dataset = []
    for candidate in candidates:
        try:
            dataset.append(build_stock_record(candidate))
        except Exception as exc:
            print(f"Failed to build dataset for {candidate['ticker']}: {exc}")
        time.sleep(0.5)

    dataset_key = f"{prefix}/stock_analysis_dataset.json"
    dataset_location = upload_json_to_s3(dataset, dataset_key)
    print(f"Raw dataset stored at: {dataset_location}")

    ai_results = []
    for record in dataset:
        ai_results.append(analyze_with_bedrock(record))
        time.sleep(0.5)

    report = create_final_report(ai_results, snapshot, candidates)
    report_key = f"{prefix}/stock_ai_report.json"
    report_location = upload_json_to_s3(report, report_key)

    print("\n" + "=" * 46 + "\n             AI RECOVERY REPORT\n" + "=" * 46)
    for index, stock in enumerate(report["ranked_candidates"], start=1):
        print(
            f"{index}. {stock.get('ticker')} - {stock.get('verdict')} | "
            f"Score: {stock.get('overall_score')} | "
            f"Probability positive: {stock.get('next_day_probability_positive_percent')}% | "
            f"Expected move: {stock.get('expected_next_day_move_percent')}% | Risk: {stock.get('risk_score')}"
        )
    print(f"Dataset: {dataset_location}\nReport: {report_location}")
    return report

# ============================ AWS LAMBDA HANDLER ============================
def lambda_handler(event, context):
    request_id = getattr(context, "aws_request_id", "local-run") if context else "local-run"
    print("=" * 46 + "\nAWS Lambda Stock AI Agent started")
    print(f"Request ID: {request_id}\n" + "=" * 46)
    try:
        report = main()
        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "Stock AI analysis completed",
                "s3_bucket": S3_BUCKET,
                "date_prefix": get_s3_prefix(),
                "stocks_scanned": report.get("stocks_scanned", 0),
                "stocks_eligible": report.get("stocks_eligible", 0),
                "stocks_analyzed": report.get("stocks_selected", 0),
            }),
        }
    except Exception as exc:
        print(f"FATAL ERROR: {exc}")
        return {
            "statusCode": 500,
            "body": json.dumps({
                "message": "Stock AI analysis failed",
                "error": str(exc),
                "date_prefix": get_s3_prefix(),
            }),
        }

if __name__ == "__main__":
    print(json.dumps(lambda_handler({}, None), indent=2))
