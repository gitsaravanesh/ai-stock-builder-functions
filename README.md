# Stock AI – GitHub Actions to Amazon ECR

This package contains the Docker build files and three GitHub Actions workflows.
GitHub-hosted Ubuntu runners build the images; Docker is not required on your
Windows/local machine.

## Application entrypoints

Each image runs `app.lambda_handler` from `app.py`.

- `before_market/app.py` — morning recovery research report
- `after_market/app.py` — same-day evaluation of that report
- `next_trading_day/app.py` — 16:00 IST preliminary next-session outlook

Shared defaults: `DROP_THRESHOLD=-1.0`, `MAX_STOCKS=5`, timezone
`Asia/Kolkata`, and S3 bucket `saravanesh-stock-ai-results`.

## Expected repository structure

    stock-ai/
    ├── before_market/
    │   ├── app.py
    │   ├── requirements.txt
    │   └── Dockerfile
    ├── after_market/
    │   ├── app.py
    │   ├── requirements.txt
    │   └── Dockerfile
    ├── next_trading_day/
    │   ├── app.py
    │   ├── requirements.txt
    │   └── Dockerfile
    └── .github/workflows/
        ├── before-market.yml
        ├── after-market.yml
        └── next-trading-day.yml

## Next trading day

`next_trading_day` is the third Lambda, `stock-next-day-function`. Run it at
16:00 IST on weekdays, after the NSE cash close (15:30 IST). It will not score
today's session before 15:45 IST.

It finds Nifty 50 names down at least 1% versus the previous session close
(same `DROP_THRESHOLD=-1.0` rule as before market; a positive value is treated
as a loss magnitude). It then writes a preliminary next-session outlook for the
largest declines, up to `MAX_STOCKS` (default 5): historical bounce rate,
RSI/SMA/MACD, and a short Bedrock increase/decrease call. It does not fetch
news or the full fundamental packet, and it does not overwrite
`stock_ai_report.json`.

S3 keys, dated in Asia/Kolkata:

- `YYYY/MM/DD/next_trading_day_dataset.json`
- `YYYY/MM/DD/next_trading_day_report.json`

Weekend invokes are skipped. If today's close is not published yet, the run
waits 60 seconds, scans once more, then skips rather than reusing yesterday.
Create an EventBridge rule in UTC:

`cron(30 10 ? * MON-FRI *)`

Create the Lambda before merging this workflow (container image, timeout at
least 10 minutes, same S3 and Bedrock permissions as `stock-function`). The
deploy step only updates function code; it does not create the function or
the schedule.

Report fields for a UI:

- Header: `title`, `generated_at`, `timezone`, `session_date`, `outlook_for`, `model`, `screening_rule`, `preliminary`, `warning`
- Coverage: `stocks_in_universe`, `stocks_with_session_bar`, `stocks_missing_session_bar`, `stocks_download_failed`, `stocks_eligible`, `stocks_selected`, `stocks_requested`, `stocks_with_outlook`, `stocks_partial`, `stocks_failed`
- Each `candidates[]` row: `ticker`, `company`, `status`, `drop_rank`, `session_date`, `previous_session_date`, `previous_close`, `close`, `change_percent`, `volume`, `rsi`, `sma20`, `sma50`, `close_vs_sma20_percent`, `macd`, `macd_signal`, `similar_drop_events`, `historical_next_day_increase_count`, `historical_next_day_increase_rate_percent`, `average_next_day_return_percent`, `historical_five_day_recovery_rate_percent`, `average_five_day_return_percent`, `historical_bias`
- Outlook, when `status` is `SUCCESS`: `will_increase`, `predicted_direction`, `probability_positive_percent`, `confidence`, `verdict`, `expected_next_day_move_percent`, `expected_range_low_percent`, `expected_range_high_percent`, `reason`, `key_risk`
- Errors: `error` on a partial/failed row, `history_error` when history could not be built

`will_increase` is true when `probability_positive_percent` is at least 50.
`confidence` is `LOW` between 40 and 60, otherwise `HIGH`. `verdict` is
`INCREASE` or `DECREASE`. `historical_bias` is the same idea from past similar
drops, and can be `UNKNOWN`.

## AWS setup

1. Create an ECR repository named `stock-ai` in `us-east-1`.
2. Create the GitHub OIDC provider in IAM:
   - URL: `https://token.actions.githubusercontent.com`
   - Audience: `sts.amazonaws.com`
3. Create an IAM role trusted only for your GitHub repository and `main` branch.
   Use the trust policy pattern below, replacing the placeholders:

    {
      "Version": "2012-10-17",
      "Statement": [{
        "Effect": "Allow",
        "Principal": {
          "Federated": "arn:aws:iam::YOUR_ACCOUNT_ID:oidc-provider/token.actions.githubusercontent.com"
        },
        "Action": "sts:AssumeRoleWithWebIdentity",
        "Condition": {
          "StringEquals": {
            "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
            "token.actions.githubusercontent.com:sub": "repo:YOUR_GITHUB_OWNER/YOUR_REPOSITORY:ref:refs/heads/main"
          }
        }
      }]
    }

4. Attach an ECR push policy to that role. Scope the repository ARN to:
   `arn:aws:ecr:us-east-1:YOUR_ACCOUNT_ID:repository/stock-ai`

   Required actions:
   - `ecr:GetAuthorizationToken` on `*`
   - `ecr:BatchCheckLayerAvailability`
   - `ecr:CompleteLayerUpload`
   - `ecr:InitiateLayerUpload`
   - `ecr:PutImage`
   - `ecr:UploadLayerPart`

## GitHub repository variables

In Settings → Secrets and variables → Actions → Variables, add:

- `AWS_REGION` = `us-east-1`
- `AWS_ROLE_ARN` = ARN of the OIDC IAM role
- `ECR_REPOSITORY` = `stock-ai`

The account ID is not needed by the workflow because the ECR login action
returns the registry URL.

## Run

1. Commit and push to the `main` branch.
2. Changes under `before_market/` trigger the before-market workflow.
3. Changes under `after_market/` trigger the after-market workflow.
4. Changes under `next_trading_day/` trigger the next-trading-day workflow.
5. You can also run any workflow manually from GitHub → Actions.

Images pushed:

- `stock-ai:before-market-latest`
- `stock-ai:before-market-<commit-sha>`
- `stock-ai:after-market-latest`
- `stock-ai:after-market-<commit-sha>`
- `stock-ai:next-trading-day-latest`
- `stock-ai:next-trading-day-<commit-sha>`

## Notes

- Docker is installed only on the temporary GitHub-hosted runner.
- No long-lived AWS access keys are stored in GitHub.
- If your application uses environment variables or secrets, add them to
  GitHub Actions/AWS securely; do not commit credentials to this repository.
- Confirm the container architecture matches your Lambda/ECS target.
