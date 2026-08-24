#!/usr/bin/env python3
"""
Amazon wishlist deal notifier.

Reads a PUBLIC Amazon wish list, tracks each item's price over time in
prices.json, and emails a summary whenever an item's current price drops
meaningfully below the highest ("regular") price we've recorded for it.

No Amazon login is used or required: the list must be shared publicly.
"""

import json
import os
import re
import smtplib
import statistics
import sys
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
HISTORY_PATH = ROOT / "prices.json"

# A realistic desktop browser UA. Amazon serves a robot check to obvious bots.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
}

MAX_PAGES = 15  # safety cap on wishlist "load more" pagination


# --------------------------------------------------------------------------- #
# Config / persistence
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(f"Missing {CONFIG_PATH.name}. Copy config.example.json to config.json.")
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not cfg.get("wishlist_url"):
        sys.exit("config.json is missing 'wishlist_url'.")
    return cfg


def load_history() -> dict:
    if HISTORY_PATH.exists():
        try:
            return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_history(history: dict) -> None:
    HISTORY_PATH.write_text(json.dumps(history, indent=2, sort_keys=True), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Scraping
# --------------------------------------------------------------------------- #
def base_domain(url: str) -> str:
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def parse_price(text: str):
    """Pull a float out of a price string like '$19.99' or '1,299.00 €'."""
    if not text:
        return None
    cleaned = text.replace(",", "")
    m = re.search(r"\d+(?:\.\d{1,2})?", cleaned)
    return float(m.group()) if m else None


def extract_items(soup: BeautifulSoup, domain: str) -> dict:
    """Return {item_id: {title, url, price}} for items on the page."""
    items = {}
    for li in soup.select("li[data-itemid]"):
        item_id = li.get("data-itemid")
        if not item_id:
            continue

        # Price: prefer the data-price attribute, fall back to the visible tag.
        price = None
        dp = li.get("data-price")
        if dp not in (None, "", "-Infinity"):
            try:
                price = float(dp)
            except ValueError:
                price = None
        if price is None:
            tag = li.select_one(".a-price .a-offscreen") or li.select_one(".a-color-price")
            if tag:
                price = parse_price(tag.get_text())

        # Title + product URL from the item-name anchor. Layouts differ by
        # marketplace: US uses id="itemName_...", others (e.g. amazon.es) use a
        # plain product link whose href points at /dp/ and carries coliid=.
        title, url = None, None
        anchor = (
            li.select_one('a[id^="itemName_"]')
            or li.select_one("h2 a[href], h3 a[href]")
            or li.select_one('a[href*="/dp/"]')
        )
        if anchor:
            title = (anchor.get("title") or anchor.get_text(strip=True) or "").strip()
            href = anchor.get("href")
            if href:
                url = urljoin(domain + "/", href)

        if not title:
            title = f"Item {item_id}"

        # Product thumbnail for the dashboard. Amazon lazy-loads some images via
        # data-* attributes, so check those before the plain src.
        image = None
        img = li.select_one("img")
        if img:
            image = (
                img.get("src")
                or img.get("data-src")
                or img.get("data-a-hires")
            )
            if image and image.startswith("//"):
                image = "https:" + image

        items[item_id] = {"title": title, "url": url, "price": price, "image": image}
    return items


def find_more_url(soup: BeautifulSoup, domain: str):
    """Locate Amazon's 'show more' pagination endpoint, if present."""
    node = soup.select_one("[data-showmoreurl]")
    if node and node.get("data-showmoreurl"):
        return urljoin(domain + "/", node["data-showmoreurl"])
    inp = soup.find("input", {"name": "showMoreUrl"})
    if inp and inp.get("value"):
        return urljoin(domain + "/", inp["value"])
    return None


def get_with_retries(session: requests.Session, url: str, tries: int = 4):
    """GET with exponential backoff on transient throttles (503/429/500)."""
    delay = 2.0
    last_exc = None
    for attempt in range(tries):
        try:
            resp = session.get(url, timeout=30)
            if resp.status_code in (503, 429, 500):
                raise requests.HTTPError(f"{resp.status_code} throttle", response=resp)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < tries - 1:
                time.sleep(delay)
                delay *= 2
    raise last_exc


def fetch_wishlist(url: str) -> dict:
    """Fetch all items across paginated wishlist loads.

    The first page is required; subsequent "load more" pages are best-effort so
    a transient failure paginating never discards items already collected.
    """
    domain = base_domain(url)
    session = requests.Session()
    session.headers.update(HEADERS)

    resp = get_with_retries(session, url)  # first page: fatal if it fails
    if "captcha" in resp.text.lower() and "wishlist" not in resp.text.lower():
        raise RuntimeError(
            "Amazon returned a robot/CAPTCHA check instead of the wishlist. "
            "Confirm the list is public and try again later."
        )

    soup = BeautifulSoup(resp.text, "html.parser")
    all_items: dict = extract_items(soup, domain)

    next_url = find_more_url(soup, domain)
    for _ in range(MAX_PAGES):
        if not next_url:
            break
        time.sleep(1.0)  # be polite between page loads
        try:
            resp = get_with_retries(session, next_url)
        except requests.RequestException as exc:
            print(f"Note: stopped paginating after a transient error ({exc}); "
                  f"keeping {len(all_items)} item(s) from earlier pages.")
            break
        soup = BeautifulSoup(resp.text, "html.parser")
        new_items = extract_items(soup, domain)
        before = len(all_items)
        all_items.update(new_items)
        more = find_more_url(soup, domain)
        # Stop if this page added nothing new or the cursor didn't advance.
        if len(all_items) == before or more == next_url:
            break
        next_url = more

    if not all_items:
        raise RuntimeError(
            "No items parsed from the wishlist. The list may be private, empty, "
            "or Amazon changed its page layout."
        )
    return all_items


# --------------------------------------------------------------------------- #
# Discount logic
# --------------------------------------------------------------------------- #
def evaluate(items: dict, history: dict, min_pct: float) -> list:
    """Update history in place; return list of newly discounted items to notify."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    deals = []

    for item_id, info in items.items():
        price = info["price"]

        # Items whose price is lazy-loaded by JS (most grocery items here) have
        # no server-side price. Skip them entirely — don't clutter the history.
        # An existing tracked record is left untouched in case the price was
        # only temporarily missing.
        if price is None:
            continue

        rec = history.get(item_id, {})
        rec["title"] = info["title"]
        rec["url"] = info["url"] or rec.get("url")
        rec["image"] = info.get("image") or rec.get("image")
        rec["last_seen"] = now

        rec["reference_price"] = max(rec.get("reference_price", 0) or 0, price)
        rec["last_price"] = price

        # Append today's price point for the dashboard's trend chart. One point
        # per day: re-runs on the same day overwrite rather than duplicate.
        ph = rec.get("price_history", [])
        if ph and ph[-1].get("date") == today:
            ph[-1]["price"] = price
        else:
            ph.append({"date": today, "price": price})
        rec["price_history"] = ph[-365:]  # keep at most a year

        # A discount is measured against the TYPICAL (median) price, not the
        # highest ever seen. Using the max let a temporary price spike ratchet
        # the reference up, so a return to normal looked like a fake discount.
        # The median is robust to such spikes. Needs a few points to be stable.
        prices_seen = [p["price"] for p in rec["price_history"]]
        baseline = statistics.median(prices_seen)
        rec["baseline_price"] = round(baseline, 2)

        discount_pct = (baseline - price) / baseline * 100 if baseline > 0 else 0
        last_notified = rec.get("last_notified_price")

        # Notify when the drop clears the threshold vs the typical price AND this
        # price is a new low relative to whatever we last alerted on (prevents
        # daily repeats). Require enough history for a meaningful baseline.
        enough_history = len(prices_seen) >= 3
        is_new_low = last_notified is None or price < last_notified
        if enough_history and discount_pct >= min_pct and is_new_low:
            rec["last_notified_price"] = price
            deals.append(
                {
                    "title": info["title"],
                    "url": rec["url"],
                    "price": price,
                    "reference": round(baseline, 2),
                    "discount_pct": round(discount_pct, 1),
                }
            )

        history[item_id] = rec

    return deals


# --------------------------------------------------------------------------- #
# Email
# --------------------------------------------------------------------------- #
def build_email_html(deals: list, sym: str = "$") -> str:
    rows = []
    for d in deals:
        link = d["url"] or "#"
        rows.append(
            f"""
            <tr>
              <td style="padding:10px 12px;border-bottom:1px solid #eee;">
                <a href="{link}" style="color:#0b5cad;text-decoration:none;font-weight:600;">{d['title']}</a>
              </td>
              <td style="padding:10px 12px;border-bottom:1px solid #eee;white-space:nowrap;">
                <span style="color:#b12704;font-weight:700;">{sym}{d['price']:.2f}</span>
                <span style="color:#767676;text-decoration:line-through;margin-left:6px;">{sym}{d['reference']:.2f}</span>
              </td>
              <td style="padding:10px 12px;border-bottom:1px solid #eee;white-space:nowrap;">
                <span style="background:#067d62;color:#fff;border-radius:4px;padding:2px 8px;font-weight:600;">-{d['discount_pct']}%</span>
              </td>
            </tr>"""
        )
    return f"""\
<div style="font-family:Arial,Helvetica,sans-serif;max-width:640px;margin:0 auto;">
  <h2 style="color:#232f3e;">🔔 {len(deals)} wishlist item{'s' if len(deals) != 1 else ''} on sale</h2>
  <p style="color:#555;">Prices dropped below their usual level on your Amazon wish list.</p>
  <table style="border-collapse:collapse;width:100%;font-size:14px;">
    <thead>
      <tr style="text-align:left;color:#767676;font-size:12px;text-transform:uppercase;">
        <th style="padding:8px 12px;">Item</th>
        <th style="padding:8px 12px;">Price</th>
        <th style="padding:8px 12px;">Off</th>
      </tr>
    </thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
  <p style="color:#999;font-size:12px;margin-top:16px;">
    Sent by your Amazon deal notifier. "Off" is measured against the highest price recorded since tracking began.
  </p>
</div>"""


def send_email(cfg: dict, deals: list) -> None:
    user = os.environ.get("GMAIL_USER")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    to_addr = cfg.get("notify_email") or user
    sym = cfg.get("currency_symbol", "$")

    if not user or not password:
        print("GMAIL_USER / GMAIL_APP_PASSWORD not set — skipping email. Deals found:")
        for d in deals:
            print(f"  - {d['title']}: ${d['price']:.2f} (-{d['discount_pct']}%)")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🔔 {len(deals)} Amazon wishlist deal(s) today"
    msg["From"] = user
    msg["To"] = to_addr

    plain = "\n".join(
        f"- {d['title']}: {sym}{d['price']:.2f} (was {sym}{d['reference']:.2f}, -{d['discount_pct']}%)\n  {d['url']}"
        for d in deals
    )
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(build_email_html(deals, sym), "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(user, password)
        server.sendmail(user, [to_addr], msg.as_string())
    print(f"Email sent to {to_addr} with {len(deals)} deal(s).")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    cfg = load_config()

    # One-off delivery test: set SEND_TEST_EMAIL=1/true to send a sample alert
    # and exit, without touching price history. Used to verify email works.
    if os.environ.get("SEND_TEST_EMAIL", "").lower() in ("1", "true", "yes"):
        print("SEND_TEST_EMAIL set — sending a sample alert email and exiting.")
        demo = [{
            "title": "✅ TEST — your Amazon deal notifier is working",
            "url": cfg["wishlist_url"],
            "price": 9.99,
            "reference": 19.99,
            "discount_pct": 50.0,
        }]
        send_email(cfg, demo)
        return

    min_pct = float(cfg.get("min_discount_pct", 5))
    history = load_history()

    print(f"Fetching wishlist: {cfg['wishlist_url']}")
    items = fetch_wishlist(cfg["wishlist_url"])
    priced = sum(1 for v in items.values() if v["price"] is not None)
    print(f"Parsed {len(items)} item(s); {priced} with a readable price, "
          f"{len(items) - priced} skipped (price loads via JavaScript).")

    deals = evaluate(items, history, min_pct)
    save_history(history)

    if deals:
        print(f"{len(deals)} new deal(s) meeting the {min_pct}% threshold.")
        send_email(cfg, deals)
    else:
        print(f"No new deals at or above the {min_pct}% threshold today.")


if __name__ == "__main__":
    main()
