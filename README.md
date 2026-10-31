# Stock AI – GitHub Actions to Amazon ECR

This package contains the Docker build files and two GitHub Actions workflows.
GitHub-hosted Ubuntu runners build the images; Docker is not required on your
Windows/local machine.

## Important: application source placeholders

`before_market/main.py` and `after_market/main.py` are placeholders, not your
actual stock-analysis/evaluation logic. The previous conversation context did
not contain the complete Python source files. Replace each placeholder with
your existing program before relying on the images. Your known before-market
configuration was: `DROP_THRESHOLD=-1.0`, `MAX_STOCKS=5`,
`HISTORY_PERIOD=5y`, `MAX_NEWS=10`, timezone `Asia/Kolkata`, and S3 bucket
`saravanesh-stock-ai-results`. Reapply these in your actual application if
they are still current.

## Expected repository structure

    stock-ai/
    ├── before_market/
    │   ├── main.py
    │   ├── requirements.txt
    │   └── Dockerfile
    ├── after_market/
    │   ├── main.py
    │   ├── requirements.txt
    │   └── Dockerfile
    └── .github/workflows/
        ├── before-market.yml
        └── after-market.yml

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

1. Replace both placeholder `main.py` files with your actual programs.
2. Commit and push to the `main` branch.
3. Changes under `before_market/` trigger the before-market workflow.
4. Changes under `after_market/` trigger the after-market workflow.
5. You can also run either workflow manually from GitHub → Actions.

Images pushed:

- `stock-ai:before-market-latest`
- `stock-ai:before-market-<commit-sha>`
- `stock-ai:after-market-latest`
- `stock-ai:after-market-<commit-sha>`

## Notes

- Docker is installed only on the temporary GitHub-hosted runner.
- No long-lived AWS access keys are stored in GitHub.
- If your application uses environment variables or secrets, add them to
  GitHub Actions/AWS securely; do not commit credentials to this repository.
- Confirm the container architecture matches your Lambda/ECS target.
