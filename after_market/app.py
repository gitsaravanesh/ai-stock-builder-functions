import json
import math
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import boto3
import numpy as np
import pandas as pd
import yfinance as yf
from botocore.config import Config


# ============================================================
# CONFIGURATION
# ============================================================

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
S3_BUCKET = os.environ.get("S3_BUCKET", "saravanesh-stock-ai-results")
LOCAL_TIMEZONE = ZoneInfo("Asia/Kolkata")
PREDICTION_FILENAME = os.environ.get("PREDICTION_FILENAME", "stock_ai_report.json")
EVALUATION_FILENAME = os.environ.get("EVALUATION_FILENAME", "evaluation.json")
HISTORY_LOOKBACK_DAYS = max(7, int(os.environ.get("HISTORY_LOOKBACK_DAYS", "15")))

AWS_CONFIG = Config(
    retries={"max_attempts": 3, "mode": "standard"},
    connect_timeout=5,
    read_timeout=30,
)
s3 = boto3.client("s3", region_name=AWS_REGION, config=AWS_CONFIG)

# yfinance may try to write timezone/cookie caches. Lambda's /tmp is writable.
try:
    yf.set_tz_cache_location("/tmp/yfinance")
except Exception as exc:
    print(f"Could not configure yfinance cache location: {exc}")


# ============================================================
# DATE / S3 HELPERS
# ============================================================

def now_ist():
    return datetime.now(LOCAL_TIMEZONE)


def get_evaluation_date():
    # The evaluator is scheduled after the NSE market close.
    return now_ist().date()


def date_prefix(value):
    return value.strftime("%Y/%m/%d")


def read_json_from_s3(key):
    print(f"Reading s3://{S3_BUCKET}/{key}")
    response = s3.get_object(Bucket=S3_BUCKET, Key=key)
    return json.loads(response["Body"].read().decode("utf-8"))


def upload_json_to_s3(payload, key):
    body = json.dumps(
        payload, indent=2, ensure_ascii=False, default=str, allow_nan=False
    )
    print(f"Uploading s3://{S3_BUCKET}/{key}")
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=body.encode("utf-8"),
        ContentType="application/json",
    )
    return f"s3://{S3_BUCKET}/{key}"


def safe_float(value):
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


# ============================================================
# MARKET PRICE LOOKUP
# ============================================================

class NoSessionBarError(ValueError):
    """Yahoo has no completed bar for the evaluation date (holiday or not yet published)."""


def _normalise_index_date(index_value):
    ts = pd.Timestamp(index_value)
    if ts.tzinfo is not None:
        # Yahoo daily bars are exchange-local. Use the date component
        # represented by the returned timestamp.
        ts = ts.tz_localize(None)
    return ts.date()


def get_reference_and_actual_close(ticker, evaluation_date):
    """
    Fetch a window ending on evaluation_date and locate:
      - latest available close strictly before evaluation_date
      - close exactly on evaluation_date

    This avoids assuming the previous calendar day was a trading day.
    """
    start_date = evaluation_date - timedelta(days=HISTORY_LOOKBACK_DAYS)
    end_date = evaluation_date + timedelta(days=1)

    print(
        f"Downloading {ticker}: "
        f"{start_date.isoformat()} through {evaluation_date.isoformat()}"
    )

    history = yf.Ticker(ticker).history(
        start=start_date.isoformat(),
        end=end_date.isoformat(),
        interval="1d",
        auto_adjust=True,
        actions=False,
    )

    if history is None or history.empty or "Close" not in history.columns:
        raise ValueError("Yahoo Finance returned no daily closing-price data")

    closes = []
    for index_value, row in history.iterrows():
        close = safe_float(row.get("Close"))
        if close is None or close <= 0:
            continue
        bar_date = _normalise_index_date(index_value)
        closes.append((bar_date, close))

    closes.sort(key=lambda item: item[0])

    actual = next(
        ((bar_date, close) for bar_date, close in closes if bar_date == evaluation_date),
        None,
    )
    previous = [
        (bar_date, close)
        for bar_date, close in closes
        if bar_date < evaluation_date
    ]

    if actual is None:
        available_dates = [d.isoformat() for d, _ in closes[-5:]]
        raise NoSessionBarError(
            f"No completed price bar found for evaluation date "
            f"{evaluation_date.isoformat()}; recent returned dates={available_dates}"
        )

    if not previous:
        raise ValueError("No previous trading-session close found")

    reference_date, reference_close = previous[-1]
    actual_date, actual_close = actual

    return {
        "reference_date": reference_date,
        "reference_close": reference_close,
        "actual_date": actual_date,
        "actual_close": actual_close,
    }


# ============================================================
# PER-STOCK EVALUATION
# ============================================================

def evaluate_candidate(candidate, evaluation_date):
    ticker = candidate.get("ticker") or "UNKNOWN"
    company = candidate.get("company") or ticker

    probability = safe_float(
        candidate.get("next_day_probability_positive_percent")
    )
    expected_move = safe_float(
        candidate.get("expected_next_day_move_percent")
    )
    range_low = safe_float(candidate.get("expected_range_low_percent"))
    range_high = safe_float(candidate.get("expected_range_high_percent"))

    result = {
        "ticker": ticker,
        "company": company,
        "status": "FAILED",
        "prediction_date": evaluation_date.isoformat(),
        "evaluation_date": evaluation_date.isoformat(),
        "positive_probability_percent": probability,
        "expected_move_percent": expected_move,
        "expected_range_low_percent": range_low,
        "expected_range_high_percent": range_high,
        "overall_score": candidate.get("overall_score"),
        "verdict": candidate.get("verdict"),
    }

    if probability is None or not 0 <= probability <= 100:
        result["error"] = "Missing or invalid next_day_probability_positive_percent"
        return result

    if expected_move is None:
        result["error"] = "Missing or invalid expected_next_day_move_percent"
        return result

    if range_low is None or range_high is None or range_low > range_high:
        result["error"] = "Missing or invalid expected range"
        return result

    try:
        prices = get_reference_and_actual_close(ticker, evaluation_date)
        reference_close = prices["reference_close"]
        actual_close = prices["actual_close"]

        actual_return = ((actual_close - reference_close) / reference_close) * 100

        if actual_return > 0:
            actual_direction = "POSITIVE"
            actual_outcome = 1.0
        elif actual_return < 0:
            actual_direction = "NEGATIVE"
            actual_outcome = 0.0
        else:
            actual_direction = "FLAT"
            actual_outcome = 0.0

        # Probability >= 50% implies the model's directional call is positive.
        predicted_direction = "POSITIVE" if probability >= 50.0 else "NEGATIVE"

        # A flat actual result is neither a positive nor negative direction.
        # Keep direction_correct null rather than marking it as a false call.
        direction_correct = (
            None if actual_direction == "FLAT"
            else predicted_direction == actual_direction
        )

        move_error = actual_return - expected_move
        inside_range = range_low <= actual_return <= range_high
        brier_error = ((probability / 100.0) - actual_outcome) ** 2

        result.update({
            "status": "SUCCESS",
            "reference_date": prices["reference_date"].isoformat(),
            "actual_date": prices["actual_date"].isoformat(),
            "reference_close": reference_close,
            "actual_close": actual_close,
            "actual_return_percent": actual_return,
            "actual_direction": actual_direction,
            "predicted_direction": predicted_direction,
            "direction_correct": direction_correct,
            "expected_move_error_percent": move_error,
            "absolute_move_error_percent": abs(move_error),
            "inside_expected_range": inside_range,
            "probability_squared_error": brier_error,
        })

    except Exception as exc:
        result["error"] = str(exc)
        result["no_session_bar"] = isinstance(exc, NoSessionBarError)
        print(f"Evaluation failed for {ticker}: {exc}")

    return result


# ============================================================
# REPORT GENERATION
# ============================================================

def build_evaluation_report(prediction_report, evaluation_date, results, prediction_key):
    successful = [item for item in results if item.get("status") == "SUCCESS"]
    directional = [
        item for item in successful
        if item.get("direction_correct") is not None
    ]
    correct = sum(1 for item in directional if item["direction_correct"])
    incorrect = len(directional) - correct

    def average(values):
        return float(np.mean(values)) if values else None

    absolute_errors = [
        item["absolute_move_error_percent"] for item in successful
        if item.get("absolute_move_error_percent") is not None
    ]
    range_hits = [
        item for item in successful
        if isinstance(item.get("inside_expected_range"), bool)
    ]
    brier_scores = [
        item["probability_squared_error"] for item in successful
        if item.get("probability_squared_error") is not None
    ]

    return {
        "generated_at": now_ist().isoformat(),
        "timezone": "Asia/Kolkata",
        "prediction_date": evaluation_date.isoformat(),
        "evaluation_date": evaluation_date.isoformat(),
        "description": "Evaluation of same-day AI stock predictions against actual market results",
        "prediction_file": f"s3://{S3_BUCKET}/{prediction_key}",
        "model": prediction_report.get("model"),
        "screening_rule": prediction_report.get("screening_rule"),
        "stocks_requested": prediction_report.get("stocks_requested"),
        "stocks_selected_in_prediction": len(
            prediction_report.get("ranked_candidates", [])
        ),
        "stocks_evaluated": len(successful),
        "stocks_failed": len(results) - len(successful),
        "correct_predictions": correct,
        "incorrect_predictions": incorrect,
        "direction_accuracy_percent": (
            round(correct / len(directional) * 100, 2)
            if directional else None
        ),
        "average_absolute_move_error_percent": average(absolute_errors),
        "range_accuracy_percent": (
            round(
                sum(1 for item in range_hits if item["inside_expected_range"])
                / len(range_hits) * 100,
                2,
            )
            if range_hits else None
        ),
        "average_probability_squared_error": average(brier_scores),
        "results": results,
    }


# ============================================================
# MAIN / LAMBDA HANDLER
# ============================================================

def main():
    evaluation_date = get_evaluation_date()
    prefix = date_prefix(evaluation_date)
    prediction_key = f"{prefix}/{PREDICTION_FILENAME}"
    evaluation_key = f"{prefix}/{EVALUATION_FILENAME}"

    print("=" * 60)
    print("STOCK-AI AFTER-MARKET EVALUATOR")
    print(f"Evaluation date (IST): {evaluation_date.isoformat()}")
    print(f"Prediction input: s3://{S3_BUCKET}/{prediction_key}")
    print("=" * 60)

    try:
        prediction_report = read_json_from_s3(prediction_key)
    except s3.exceptions.NoSuchKey:
        return {
            "status": "SKIPPED",
            "message": "Today's prediction report was not found; no evaluation generated.",
            "prediction_key": prediction_key,
        }
    except Exception as exc:
        # S3 commonly reports missing objects as ClientError/404 rather than
        # the generated NoSuchKey class. Detect that case without hiding others.
        response = getattr(exc, "response", {})
        error_code = response.get("Error", {}).get("Code")
        if error_code in ("NoSuchKey", "404", "NotFound"):
            return {
                "status": "SKIPPED",
                "message": "Today's prediction report was not found; no evaluation generated.",
                "prediction_key": prediction_key,
            }
        raise

    candidates = prediction_report.get("ranked_candidates", [])
    if not isinstance(candidates, list):
        raise ValueError("Prediction report ranked_candidates must be a list")

    print(f"Stocks found in prediction report: {len(candidates)}")

    results = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            results.append({
                "ticker": "UNKNOWN",
                "status": "FAILED",
                "error": "Candidate record is not a JSON object",
            })
            continue
        ticker = candidate.get("ticker", "UNKNOWN")
        print(f"\nEvaluating {ticker}...")
        results.append(evaluate_candidate(candidate, evaluation_date))

    # Every stock lacking today's bar means no session was evaluated, not that
    # every prediction failed. Don't write a misleading evaluation.json.
    if results and all(item.get("no_session_bar") for item in results):
        message = (
            f"No completed market session for {evaluation_date.isoformat()} "
            "(holiday, or Yahoo has not published today's close). No evaluation written."
        )
        print(f"SKIPPED: {message}")
        return {
            "status": "SKIPPED",
            "message": message,
            "evaluation_date": evaluation_date.isoformat(),
        }

    report = build_evaluation_report(
        prediction_report, evaluation_date, results, prediction_key
    )
    location = upload_json_to_s3(report, evaluation_key)

    print("\n" + "=" * 60)
    print("EVALUATION SUMMARY")
    print(f"Evaluated successfully: {report['stocks_evaluated']}")
    print(f"Failed: {report['stocks_failed']}")
    print(f"Direction accuracy: {report['direction_accuracy_percent']}")
    print(f"Average absolute move error: {report['average_absolute_move_error_percent']}")
    print(f"Range accuracy: {report['range_accuracy_percent']}")
    print(f"Saved: {location}")
    print("=" * 60)

    return {
        "status": "COMPLETED",
        "evaluation_key": evaluation_key,
        "evaluation_location": location,
        "stocks_evaluated": report["stocks_evaluated"],
        "stocks_failed": report["stocks_failed"],
        "direction_accuracy_percent": report["direction_accuracy_percent"],
    }


def lambda_handler(event, context):
    request_id = getattr(context, "aws_request_id", "local-run") if context else "local-run"
    print(f"After-market Lambda started. Request ID: {request_id}")
    try:
        result = main()
        return {
            "statusCode": 200,
            "body": json.dumps(result, default=str),
        }
    except Exception as exc:
        print(f"FATAL EVALUATION ERROR: {exc}")
        return {
            "statusCode": 500,
            "body": json.dumps({
                "status": "FAILED",
                "error": str(exc),
            }),
        }


if __name__ == "__main__":
    print(json.dumps(lambda_handler({}, None), indent=2))
