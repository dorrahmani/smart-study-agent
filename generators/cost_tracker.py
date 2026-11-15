"""
cost_tracker.py — Real API billing cost fetchers.
Fetches actual month-to-date spend from OpenAI, Anthropic, and Google Cloud.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env", override=True)
except ImportError:
    pass


def _month_start() -> str:
    """Returns YYYY-MM-01 for the current month (UTC)."""
    now = datetime.now(tz=timezone.utc)
    return now.strftime("%Y-%m-01")


def _today() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


# ─────────────────────────────────────────────────────────────────────────────
# OpenAI
# ─────────────────────────────────────────────────────────────────────────────

def fetch_openai_real_cost(start_date: str | None = None, end_date: str | None = None) -> dict | None:
    """
    Fetch real OpenAI usage cost from the billing API.

    Defaults to current-month-to-date if no dates are given.

    Returns dict with:
        total_usd  (float)
        by_date    (dict[str, float])
        currency   "USD"
        start_date (str)
        end_date   (str)
    or None on error.
    """
    try:
        import requests
    except ImportError:
        return None

    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return None

    start_date = start_date or _month_start()
    end_date   = end_date   or _today()

    headers = {"Authorization": f"Bearer {api_key}"}

    # Primary: dashboard billing endpoint
    url = (
        "https://api.openai.com/dashboard/billing/usage"
        f"?start_date={start_date}&end_date={end_date}"
    )
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            return _parse_openai_billing(resp.json(), start_date, end_date)
    except Exception:
        pass

    # Fallback: per-day /v1/usage endpoint
    by_date: dict[str, float] = {}
    total_usd = 0.0
    from datetime import date as _date
    current = datetime.strptime(start_date, "%Y-%m-%d").date()
    end_d   = datetime.strptime(end_date,   "%Y-%m-%d").date()

    while current <= end_d:
        day_str = current.strftime("%Y-%m-%d")
        try:
            r = requests.get(
                f"https://api.openai.com/v1/usage?date={day_str}",
                headers=headers, timeout=8,
            )
            if r.status_code == 200:
                cents = r.json().get("total_usage", 0.0)
                usd = cents / 100.0
                by_date[day_str] = round(usd, 6)
                total_usd += usd
        except Exception:
            pass
        current += timedelta(days=1)

    if not by_date and total_usd == 0:
        return None

    return {
        "total_usd":  round(total_usd, 4),
        "by_date":    by_date,
        "currency":   "USD",
        "start_date": start_date,
        "end_date":   end_date,
    }


def _parse_openai_billing(data: dict, start_date: str, end_date: str) -> dict | None:
    try:
        daily_costs = data.get("daily_costs", [])
        by_date: dict[str, float] = {}
        total_usd = 0.0

        for day in daily_costs:
            ts = day.get("timestamp")
            if ts is None:
                continue
            day_str  = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
            day_cost = sum(item.get("cost", 0.0) for item in day.get("line_items", []))
            by_date[day_str] = round(day_cost, 6)
            total_usd += day_cost

        if not by_date:
            total_cents = data.get("total_usage", 0.0)
            total_usd   = total_cents / 100.0

        return {
            "total_usd":  round(total_usd, 4),
            "by_date":    by_date,
            "currency":   "USD",
            "start_date": start_date,
            "end_date":   end_date,
        }
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Anthropic (Claude)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_claude_real_cost(start_date: str | None = None, end_date: str | None = None) -> dict | None:
    """
    Fetch real Anthropic/Claude usage cost.

    Defaults to current-month-to-date if no dates are given.
    """
    try:
        import requests
    except ImportError:
        return None

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None

    start_date = start_date or _month_start()
    end_date   = end_date   or _today()

    headers = {
        "x-api-key":         api_key,
        "anthropic-version": "2023-06-01",
        "content-type":      "application/json",
    }

    by_date: dict[str, float] = {}
    total_usd = 0.0

    for endpoint in [
        f"https://api.anthropic.com/v1/usage?start_date={start_date}&end_date={end_date}",
        f"https://api.anthropic.com/v1/usage/tokens?start_date={start_date}&end_date={end_date}",
    ]:
        try:
            resp = requests.get(endpoint, headers=headers, timeout=10)
        except Exception:
            continue

        if resp.status_code == 200:
            data  = resp.json()
            daily = data.get("daily", data.get("data", data.get("usage", [])))

            if isinstance(daily, list):
                for entry in daily:
                    date_str = entry.get("date") or entry.get("timestamp")
                    if date_str and len(str(date_str)) == 10:
                        cost = float(entry.get("cost_usd", entry.get("cost", 0.0)))
                        by_date[date_str] = round(by_date.get(date_str, 0.0) + cost, 6)
                        total_usd += cost

            if not by_date:
                top = data.get("total_cost_usd", data.get("total_cost", data.get("cost_usd")))
                if top is not None:
                    total_usd = float(top)

            if by_date or total_usd > 0:
                return {
                    "total_usd":  round(total_usd, 4),
                    "by_date":    by_date,
                    "currency":   "USD",
                    "start_date": start_date,
                    "end_date":   end_date,
                }

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Google Cloud / Gemini
# ─────────────────────────────────────────────────────────────────────────────

def fetch_google_real_cost(start_date: str | None = None, end_date: str | None = None) -> dict | None:
    """
    Fetch real Google Cloud / Gemini API spend for the billing account.

    Requires ONE of the following env vars:
      GOOGLE_APPLICATION_CREDENTIALS  — path to service account JSON
      GOOGLE_CLOUD_BILLING_ACCOUNT    — billing account ID (XXXXXX-XXXXXX-XXXXXX)

    Uses the Cloud Billing Budget API or falls back to the GenerativeLanguage
    usage endpoint if only GEMINI_API_KEY is present.

    Returns same format as the other fetchers, or None if not configured.
    """
    start_date = start_date or _month_start()
    end_date   = end_date   or _today()

    # ── Strategy A: google-cloud-billing SDK ──────────────────────────────────
    result = _fetch_google_via_sdk(start_date, end_date)
    if result is not None:
        return result

    # ── Strategy B: REST with service account JSON credentials ────────────────
    result = _fetch_google_via_rest(start_date, end_date)
    if result is not None:
        return result

    # ── Strategy C: Gemini generativelanguage usage (API key only) ───────────
    result = _fetch_gemini_usage(start_date, end_date)
    if result is not None:
        return result

    return None


def _fetch_google_via_sdk(start_date: str, end_date: str) -> dict | None:
    """Try google-cloud-billing Python SDK."""
    billing_account = os.getenv("GOOGLE_CLOUD_BILLING_ACCOUNT", "")
    if not billing_account:
        return None
    try:
        from google.cloud import billing_v1  # type: ignore
        client = billing_v1.CloudBillingClient()
        # list budgets — if this works, auth is set up
        name = f"billingAccounts/{billing_account.lstrip('billingAccounts/')}"
        _ = list(client.list_billing_accounts())  # sanity check auth
    except Exception:
        return None

    # Use Cloud Billing Reports API (requires BigQuery export normally).
    # If SDK is present, try to get budget info at least.
    try:
        from google.cloud import billing_budgets_v1  # type: ignore
        budget_client = billing_budgets_v1.BudgetServiceClient()
        budgets = list(budget_client.list_budgets(parent=name))
        if budgets:
            # Sum spend_basis from all budgets (approximation)
            total = 0.0
            for b in budgets:
                if hasattr(b, "amount") and hasattr(b.amount, "last_period_amount"):
                    pass  # not actual spend
                # Try allUpdatesRule / notificationsRule for spend
            # SDK doesn't expose actual spend directly — return None to try REST
    except Exception:
        pass
    return None


def _fetch_google_via_rest(start_date: str, end_date: str) -> dict | None:
    """Try Google Cloud Cost Management REST API with service account credentials."""
    creds_path   = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
    project_id   = os.getenv("GOOGLE_CLOUD_PROJECT_ID", os.getenv("GOOGLE_CLOUD_PROJECT", ""))
    billing_acct = os.getenv("GOOGLE_CLOUD_BILLING_ACCOUNT", "")

    if not creds_path and not billing_acct:
        return None

    try:
        import requests
        import json as _json
    except ImportError:
        return None

    # Get an access token via service account
    token = _google_get_access_token(creds_path)
    if not token:
        return None

    if not billing_acct:
        return None

    # Cloud Billing Reports: list cost data by time
    # NOTE: The proper endpoint for cost data requires BigQuery export.
    # The REST billing API /v1beta/billingAccounts/{id}/reports is available
    # but only returns budget amounts, not actual spend.
    # We use the Cloud Usage Cost Reports API (v1beta).
    url = (
        f"https://cloudbilling.googleapis.com/v1beta/"
        f"billingAccounts/{billing_acct}/reports"
        f"?dateRange.startDate.year={start_date[:4]}"
        f"&dateRange.startDate.month={int(start_date[5:7])}"
        f"&dateRange.startDate.day={int(start_date[8:10])}"
        f"&dateRange.endDate.year={end_date[:4]}"
        f"&dateRange.endDate.month={int(end_date[5:7])}"
        f"&dateRange.endDate.day={int(end_date[8:10])}"
    )

    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=12,
        )
        if resp.status_code == 200:
            data = resp.json()
            total = float(data.get("costSummary", {}).get("totalCost", {}).get("units", 0))
            nanos = data.get("costSummary", {}).get("totalCost", {}).get("nanos", 0)
            total += nanos / 1e9
            return {
                "total_usd":  round(total, 4),
                "by_date":    {},
                "currency":   data.get("costSummary", {}).get("totalCost", {}).get("currencyCode", "USD"),
                "start_date": start_date,
                "end_date":   end_date,
            }
    except Exception:
        pass

    return None


def _google_get_access_token(creds_path: str) -> str | None:
    """Exchange service account JSON for a short-lived access token."""
    if not creds_path or not Path(creds_path).exists():
        return None
    try:
        import json as _json
        import time
        import base64
        import hmac
        import hashlib
        import requests

        creds = _json.loads(Path(creds_path).read_text())
        if creds.get("type") != "service_account":
            return None

        # Build JWT
        now = int(time.time())
        header  = base64.urlsafe_b64encode(b'{"alg":"RS256","typ":"JWT"}').rstrip(b"=")
        payload = base64.urlsafe_b64encode(_json.dumps({
            "iss": creds["client_email"],
            "scope": "https://www.googleapis.com/auth/cloud-platform",
            "aud":  "https://oauth2.googleapis.com/token",
            "iat":  now,
            "exp":  now + 3600,
        }).encode()).rstrip(b"=")

        # Sign with RSA private key
        from cryptography.hazmat.primitives import serialization, hashes  # type: ignore
        from cryptography.hazmat.primitives.asymmetric import padding       # type: ignore
        from cryptography.hazmat.backends import default_backend            # type: ignore

        private_key = serialization.load_pem_private_key(
            creds["private_key"].encode(),
            password=None,
            backend=default_backend(),
        )
        msg = header + b"." + payload
        sig = private_key.sign(msg, padding.PKCS1v15(), hashes.SHA256())
        jwt = msg + b"." + base64.urlsafe_b64encode(sig).rstrip(b"=")

        resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion":  jwt.decode(),
            },
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("access_token")
    except Exception:
        pass
    return None


def _fetch_gemini_usage(start_date: str, end_date: str) -> dict | None:
    """
    Fallback: if only GEMINI_API_KEY is set, try the Generative Language
    usage endpoint. Note: Google doesn't expose a public usage/billing REST
    endpoint for API-key-only auth — this returns None unless a future
    endpoint is added.
    """
    api_key = os.getenv("GEMINI_API_KEY", os.getenv("GOOGLE_API_KEY", ""))
    if not api_key:
        return None

    # Google does not currently expose spend data via API key alone.
    # Return a sentinel so the UI can show "configure billing account".
    return {
        "total_usd":  None,   # None = "API key found but billing unavailable"
        "by_date":    {},
        "currency":   "USD",
        "start_date": start_date,
        "end_date":   end_date,
        "note":       "Google billing requires GOOGLE_CLOUD_BILLING_ACCOUNT + service account credentials. Set GOOGLE_CLOUD_BILLING_ACCOUNT in .env to enable.",
    }
