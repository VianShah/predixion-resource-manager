# Predixion Resource Manager

One dashboard for what Sarvam, AWS, Interakt and Vobiz are being used for, with a fix guide for every alert.

- **Usage by purpose**: every service view breaks spend into client · product · purpose. Spend that matches no rule shows as *Unassigned* so you can see the gap.
- **Fix guide**: each alert has a "How to fix" link to a runbook built from the vendor's documentation.
- Money is shown in ₹ (AWS converted at `USD_INR`). Phone numbers are masked before they are stored.

## What each service can and cannot do

| Service | How data arrives | Limits |
|---|---|---|
| AWS | Cost Explorer + Billing credits, pulled hourly | Needs cost-allocation tags (`client`, `product`, `env`) activated in Billing. Credits call: VERIFY endpoint and IAM action. |
| Vobiz | Call records, balance and concurrency pulled hourly | Rate, status and 401 causes are not documented: VERIFY. Quality alert thresholds are this tool's assumptions. |
| Interakt | Signed webhooks pushed to `/webhooks/interakt` | Needs a public HTTPS URL. Missed events are not replayed. Per-message rates in `rates.json` are **placeholders**: FOR FOUNDER REVIEW. Wallet balance is entered by hand. |
| Sarvam | Your agents report each call with `rm/sarvam_logger.py` | Sarvam has no usage API, so cost is estimated from `rates.json` (VERIFY). Credits are entered by hand. No Sarvam key is needed here. |

## Set up

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then fill in .env; never commit it
python -m pytest -q         # 15 tests
RM_DEMO=1 python server.py  # try it with synthetic data at http://127.0.0.1:8080
```

For real data: fill `.env`, edit `purposes.json` (your campaign IDs, caller numbers, Sarvam workspaces, template prefixes), then:

```bash
python -m rm.collect --vendor all          # AWS + Vobiz
python -m rm.balance sarvam credits 21000  # no API for these two
python -m rm.balance interakt wallet 6200
python server.py
```

Schedule `python -m rm.collect --vendor all` hourly (cron, or an EventBridge rule on the instance that runs this).

### Interakt webhook
In Interakt Developer Settings set the webhook URL to `https://<your-host>/webhooks/interakt` and put the same secret in `INTERAKT_WEBHOOK_SECRET`. Requests without a valid signature get 401.

### Sarvam usage from your agents
Set `RM_DASHBOARD_URL` and `RM_INGEST_TOKEN` in the agent's environment, then call `log_usage(api="tts", model="bulbul:v3", units={"chars": 420}, workspace="kollect-prod")` after each Sarvam call (also on errors, with `http_status=402` etc., so they show up with their fix).

### Minimal AWS permissions
Read-only: `ce:GetCostAndUsage`, `ce:GetCostForecast`, plus the Billing credits read action (VERIFY the exact action name for `GetCredits`). Cost Explorer must be enabled once in the management account. Billing APIs are called in `us-east-1`.

## Security
- **The dashboard has no login.** It binds to `127.0.0.1` by default. Put it behind SSO or a VPN before setting `RM_HOST` to anything else.
- Write endpoints (`/api/collect`, `/api/ingest/sarvam`, `/api/balance`) need `Authorization: Bearer $RM_INGEST_TOKEN`.
- Secrets are read from `.env` only; `.env` and the database are in `.gitignore`.
- Borrower data falls under the DPDP Act 2023: only masked numbers are stored. Do not add more fields without a review.

## Files
`rm/collectors/` one module per vendor · `rm/summary.py` numbers for the dashboard · `rm/alerts.py` alert rules and thresholds · `runbooks.json` the fix guide · `purposes.json` purpose rules · `rates.json` price tables · `web/index.html` the dashboard · `tools/build_preview.py` single-file demo.

## VERIFY list
Billing `GetCredits` endpoint/IAM action/boto3 client · Vobiz rate and status fields, 401 causes, zero-balance behaviour · Sarvam prices and plan limits · Interakt rates, webhook auto-disable rule, wallet API · every runbook marked VERIFY.
