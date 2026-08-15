# Amazon Wishlist Deal Notifier

Checks a **public** Amazon wish list once a day and emails you when an item's
price drops meaningfully below the highest price it has recorded for that item.
Runs free on GitHub Actions — no server, no Amazon login.

## How it works

1. `checker.py` fetches your public wishlist page and reads each item's price.
2. It compares each price to a rolling "regular price" baseline stored in
   `prices.json` (the highest price seen so far).
3. If the current price is at least `min_discount_pct` below that baseline — and
   it's a new low since the last alert — the item is emailed to you.
4. `prices.json` is committed back to the repo so history survives between runs.

You are only alerted **once per new low**, so a steady sale price won't spam you
daily; a further drop will re-alert.

## One-time setup

### 1. Make your Amazon wish list public
Amazon → **Account → Your Lists → (your list) → ⋯ → Manage list → Privacy: Public**.
Then copy the **Share** link. It looks like
`https://www.amazon.com/hz/wishlist/ls/XXXXXXXXXXXXX`.

Paste it into `config.json` as `wishlist_url`. Also set `notify_email` and, if you
like, tune `min_discount_pct` (default 5).

### 2. Create a Gmail App Password
Requires 2-Step Verification on your Google account.
Google Account → **Security → 2-Step Verification → App passwords** → generate one
(name it "amazon notifier"). You get a 16-character password. Keep it handy.

### 3. Push this folder to a GitHub repo
```bash
git init
git add .
git commit -m "Amazon wishlist deal notifier"
git branch -M main
git remote add origin https://github.com/<you>/amazon-deal-notifier.git
git push -u origin main
```

### 4. Add repo secrets
In the repo: **Settings → Secrets and variables → Actions → New repository secret**:
- `GMAIL_USER` = `amir476@gmail.com`
- `GMAIL_APP_PASSWORD` = the 16-char app password from step 2

### 5. Run it once by hand
**Actions** tab → **Daily Amazon deal check** → **Run workflow**. The first run just
records baseline prices (no email unless something is already discounted vs itself).
After that it runs automatically every day at the cron time in
`.github/workflows/daily.yml` (13:00 UTC by default — edit to taste).

## Run locally (optional)
```bash
pip install -r requirements.txt
# PowerShell:
$env:GMAIL_USER="amir476@gmail.com"; $env:GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"
python checker.py
```
Without the two env vars set, it prints deals to the console instead of emailing.

Run the offline logic test anytime with `python test_logic.py`.

## Caveats & limitations
- **Public wishlist only.** Reading a logged-in cart/Saved-for-later would need
  your Amazon credentials and breaks on CAPTCHA/2FA — not supported here.
- **Only items with a server-rendered price are tracked.** Many low-cost grocery
  items (produce, DIA staples, etc.) have prices that Amazon fills in with
  JavaScript after the page loads; a plain-HTTP scraper cannot see those, so they
  are skipped. Higher-value items (electronics, household goods) usually have a
  price in the HTML and are tracked normally. Reading the JS-loaded prices would
  require running a headless browser (Playwright) — deliberately not used here to
  keep the job fast and robust.
- **Per-run coverage varies.** Amazon serves different page layouts per request,
  so the number of items seen on any single run fluctuates. This is harmless:
  `prices.json` only ever adds/updates entries and never deletes them, so
  baselines accumulate and persist. An item missed on one run is picked up on a
  later run.
- Amazon can change its page layout or occasionally serve a robot check to
  datacenter IPs (GitHub's runners). If parsing stops working, the item selectors
  in `extract_items()` are the thing to update.
- "Discount" is measured against the highest price seen **since tracking began**,
  not Amazon's list price. Give it a few days to learn real baselines.
