import asyncio
import json
import os
import re
import sys
import requests

BEACON_URL   = os.environ["BEACON_URL"].rstrip("/")
BEACON_SECRET = os.environ["BEACON_SECRET"]
TW_USERNAME  = os.environ["TWITTER_USERNAME"]
TW_EMAIL     = os.environ["TWITTER_EMAIL"]
TW_PASSWORD  = os.environ["TWITTER_PASSWORD"]
TW_COOKIES   = os.environ.get("TWITTER_COOKIES", "")
GH_TOKEN     = os.environ.get("GH_TOKEN", "")
GH_REPO      = os.environ.get("GH_REPO", "")

MIN_VALUE_USD = 50_000


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
    if not ticker:
        return None
    if alert_type not in ("insider_buy", "insider_sell", "large_ownership"):
        return None

    body    = alert.get("body") or ""
    value   = extract_value(body)
    company = alert.get("company_name") or ticker

    if alert_type in ("insider_buy", "insider_sell") and value < MIN_VALUE_USD:
        return None

    val_str  = format_value(value) if value else ""
    role     = extract_role(body)
    person   = extract_person(body)
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


def get_pending_alerts() -> list[dict]:
    r = requests.get(
        f"{BEACON_URL}/api/ingest/pending-tweets",
        headers={"X-Beacon-Secret": BEACON_SECRET},
        params={"limit": 10},
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
    # Try PATCH first (update), fall back to POST (create)
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

    # Load saved cookies if available
    cookies_loaded = False
    if TW_COOKIES.strip():
        try:
            client.set_cookies(json.loads(TW_COOKIES))
            cookies_loaded = True
            print("Loaded saved cookies.")
        except Exception as e:
            print(f"Cookie load failed ({e}), will re-login.")

    if not cookies_loaded:
        print("Logging in with username/password...")
        await client.login(
            auth_info_1=TW_USERNAME,
            auth_info_2=TW_EMAIL,
            password=TW_PASSWORD,
        )
        new_cookies = json.dumps(client.get_cookies())
        update_gh_variable("TWITTER_COOKIES", new_cookies)
        print("Saved new session cookies to GitHub variable.")

    alerts = get_pending_alerts()
    print(f"Found {len(alerts)} pending alert(s).")

    posted = 0
    for alert in alerts:
        tweet_text = build_tweet_text(alert)
        if not tweet_text:
            mark_tweeted(alert["id"])  # skip non-tweetable, mark so we don't re-check
            continue

        try:
            await client.create_tweet(text=tweet_text)
            mark_tweeted(alert["id"])
            print(f"  Posted: [{alert['id']}] {alert.get('ticker')} {alert.get('alert_type')}")
            posted += 1
            await asyncio.sleep(4)  # stay well within rate limits
        except Exception as e:
            print(f"  FAILED [{alert['id']}]: {e}")
            # Refresh cookies on auth errors and try to save
            try:
                new_cookies = json.dumps(client.get_cookies())
                update_gh_variable("TWITTER_COOKIES", new_cookies)
            except Exception:
                pass
            break  # stop on error, retry next run

    print(f"Done. Posted {posted} tweet(s).")


if __name__ == "__main__":
    asyncio.run(main())
