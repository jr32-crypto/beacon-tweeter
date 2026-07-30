import asyncio
import json
import os
import re
import sys
from datetime import datetime, timezone
import requests

BEACON_URL    = os.environ["BEACON_URL"].rstrip("/")
BEACON_SECRET = os.environ["BEACON_SECRET"]
TW_COOKIES    = os.environ.get("TWITTER_COOKIES", "")
GH_TOKEN      = os.environ.get("GH_TOKEN", "")
GH_REPO       = os.environ.get("GH_REPO", "")

MIN_VALUE_USD = 50_000
PER_RUN_CAP   = 3
DAILY_CAP     = 20
TWEET_GAP_SEC = 25


def extract_value(body: str) -> float:
    if not body:
        return 0
    m = re.search(r'value[:\s]+\$?([\d,]+)', body, re.I)
    return float(m.group(1).replace(",", "")) if m else 0


def format_value(usd: float) -> str:
    if usd >= 1_000_000:
        return f"${usd / 1_000_000:.1f}M"
    if usd >= 1_000:
        return f"${round(usd / 1_000)}K"
    return f"${usd:,.0f}"


def extract_person(body: str) -> str:
    if not body:
        return ""
    m = re.search(r'insider[:\s]+([^\n]+)', body, re.I)
    if not m:
        return ""
    raw = m.group(1).strip()
    if "," in raw:
        last, first = raw.split(",", 1)
        return f"{first.strip()} {last.strip()}".title()[:30]
    return raw[:30]


def extract_role(body: str) -> str:
    if not body:
        return "Insider"
    m = re.search(r'role[:\s]+([^\n]+)', body, re.I)
    if not m:
        return "Insider"
    role = m.group(1).strip()
    for label, pattern in [
        ("CEO", r"CEO|Chief Executive"),
        ("CFO", r"CFO|Chief Financial"),
        ("COO", r"COO|Chief Operating"),
        ("CTO", r"CTO|Chief Technology"),
        ("President", r"President"),
        ("Director", r"Director"),
        ("Chair", r"Chair"),
    ]:
        if re.search(pattern, role, re.I):
            return label
    return "Insider"


def build_tweet_text(alert: dict) -> str | None:
    alert_type = alert.get("alert_type", "")
    ticker     = alert.get("ticker")
    if not ticker or ticker.upper() in ("N/A", "NA", "NULL", "NONE"):
        return None
    if alert_type not in ("insider_buy", "insider_sell", "large_ownership"):
        return None

    body    = alert.get("body") or ""
    value   = extract_value(body)
    company = alert.get("company_name") or ticker

    if alert_type in ("insider_buy", "insider_sell") and value < MIN_VALUE_USD:
        return None

    val_str    = format_value(value) if value else ""
    role       = extract_role(body)
    person     = extract_person(body)
    person_str = f", {person}," if person else ""

    if alert_type == "insider_buy":
        if value >= 5_000_000:
            text = (
                f"🚨 ${ticker} — {val_str} INSIDER BUY\n\n"
                f"{role} of {company} just made one of the largest insider purchases we've tracked\n\n"
                f"${ticker} #InsiderTrading #BeaconAlerts"
            )
        elif value >= 1_000_000:
            text = (
                f"🟢 ${ticker} {role} just bought {val_str} of their own stock\n\n"
                f"{company}\n\n"
                f"${ticker} #InsiderTrading #BeaconAlerts"
            )
        else:
            text = (
                f"🟢 ${ticker} — Insider Buy\n\n"
                f"{role} of {company}{person_str} purchased shares ({val_str})\n\n"
                f"${ticker} #InsiderTrading #BeaconAlerts"
            )

    elif alert_type == "insider_sell":
        if value >= 5_000_000:
            text = (
                f"🔴 ${ticker} — {val_str} INSIDER SALE\n\n"
                f"{role} of {company} sold a significant stake\n\n"
                f"${ticker} #InsiderTrading #BeaconAlerts"
            )
        elif value >= 1_000_000:
            text = (
                f"🔴 ${ticker} {role} sold {val_str} in stock\n\n"
                f"{company}\n\n"
                f"${ticker} #InsiderTrading #BeaconAlerts"
            )
        else:
            text = (
                f"🔴 ${ticker} — Insider Sale\n\n"
                f"{role} of {company}{person_str} sold shares ({val_str})\n\n"
                f"${ticker} #InsiderTrading #BeaconAlerts"
            )

    else:  # large_ownership
        text = (
            f"🏦 ${ticker} — 5%+ Ownership Disclosure\n\n"
            f"{company}: New significant stake disclosed via SEC filing\n\n"
            f"${ticker} #InsiderTrading #BeaconAlerts"
        )

    return text[:280] if len(text) <= 280 else text[:277] + "..."


def get_daily_count() -> tuple[str, int]:
    """Return (today_str, count_so_far) from the TWEETS_TODAY GitHub variable."""
    if not GH_TOKEN or not GH_REPO:
        return ("", 0)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    owner, repo = GH_REPO.split("/", 1)
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/variables/TWEETS_TODAY"
    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    r = requests.get(url, headers=headers, timeout=10)
    if r.status_code == 200:
        raw = r.json().get("value", "")
        # format: "YYYY-MM-DD:count"
        if ":" in raw:
            date_part, count_part = raw.split(":", 1)
            if date_part == today:
                return (today, int(count_part))
    return (today, 0)


def save_daily_count(today: str, count: int) -> None:
    update_gh_variable("TWEETS_TODAY", f"{today}:{count}")


def get_pending_alerts() -> list[dict]:
    r = requests.get(
        f"{BEACON_URL}/api/ingest/pending-tweets",
        headers={"X-Beacon-Secret": BEACON_SECRET},
        params={"limit": PER_RUN_CAP},
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("alerts", [])


def mark_tweeted(alert_id: int) -> None:
    requests.post(
        f"{BEACON_URL}/api/ingest/mark-tweeted/{alert_id}",
        headers={"X-Beacon-Secret": BEACON_SECRET},
        timeout=10,
    ).raise_for_status()


def update_gh_variable(name: str, value: str) -> None:
    if not GH_TOKEN or not GH_REPO:
        return
    owner, repo = GH_REPO.split("/", 1)
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/variables/{name}"
    headers = {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    r = requests.patch(url, json={"name": name, "value": value}, headers=headers, timeout=10)
    if r.status_code == 404:
        requests.post(
            f"https://api.github.com/repos/{owner}/{repo}/actions/variables",
            json={"name": name, "value": value},
            headers=headers,
            timeout=10,
        )


async def main() -> None:
    try:
        from twikit import Client
    except ImportError:
        try:
            from twifork import Client
        except ImportError:
            print("twikit/twifork not installed")
            sys.exit(1)

    client = Client("en-US")

    if not TW_COOKIES.strip() or TW_COOKIES.strip().lower() == "none":
        print("ERROR: TWITTER_COOKIES variable is empty. Set it to {\"auth_token\": \"...\", \"ct0\": \"...\"}")
        sys.exit(1)

    try:
        client.set_cookies(json.loads(TW_COOKIES))
        print("Loaded Twitter cookies.")
    except Exception as e:
        print(f"ERROR: Failed to parse TWITTER_COOKIES: {e}")
        sys.exit(1)

    today, daily_count = get_daily_count()
    remaining_today = DAILY_CAP - daily_count
    if remaining_today <= 0:
        print(f"Daily cap reached ({daily_count}/{DAILY_CAP}). Skipping run.")
        return

    print(f"Daily count: {daily_count}/{DAILY_CAP} ({remaining_today} remaining today).")

    alerts = get_pending_alerts()
    print(f"Found {len(alerts)} pending alert(s).")

    posted = 0
    for alert in alerts:
        if posted >= min(PER_RUN_CAP, remaining_today):
            break

        tweet_text = build_tweet_text(alert)
        if not tweet_text:
            mark_tweeted(alert["id"])
            continue

        try:
            await client.create_tweet(text=tweet_text)
            mark_tweeted(alert["id"])
            daily_count += 1
            save_daily_count(today, daily_count)
            print(f"  Posted: [{alert['id']}] {alert.get('ticker')} {alert.get('alert_type')} (today: {daily_count}/{DAILY_CAP})")
            posted += 1
            if posted < min(PER_RUN_CAP, remaining_today):
                await asyncio.sleep(TWEET_GAP_SEC)
        except Exception as e:
            print(f"  FAILED [{alert['id']}]: {e}")
            try:
                new_cookies = json.dumps(client.get_cookies())
                update_gh_variable("TWITTER_COOKIES", new_cookies)
            except Exception:
                pass
            break

    print(f"Done. Posted {posted} tweet(s) this run.")


if __name__ == "__main__":
    asyncio.run(main())
