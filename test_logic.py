"""Offline smoke test for parsing + discount logic (no network, no email).

Discounts are measured against the TYPICAL (median) price over recorded
history, and only after >=3 data points exist — so a temporary price spike
can't create a phantom discount when the price returns to normal.
"""
from bs4 import BeautifulSoup
import checker

SAMPLE = """
<html><body>
<ul id="g-items">
  <li data-itemid="AAA111" data-price="19.99" class="g-item-sortable">
    <h2><a id="itemName_AAA111" title="USB-C Cable 6ft" href="/dp/B00CABLE?ref=x">USB-C Cable 6ft</a></h2>
    <span class="a-price"><span class="a-offscreen">$19.99</span></span>
  </li>
  <li data-itemid="BBB222" data-price="120.00" class="g-item-sortable">
    <h2><a id="itemName_BBB222" title="Mechanical Keyboard" href="/dp/B00KEYB">Mechanical Keyboard</a></h2>
    <span class="a-price"><span class="a-offscreen">$120.00</span></span>
  </li>
  <li data-itemid="CCC333" data-price="-Infinity" class="g-item-sortable">
    <h2><a id="itemName_CCC333" title="Out of stock thing" href="/dp/B00OOS">Out of stock thing</a></h2>
  </li>
</ul>
</body></html>
"""


def hist(prices):
    """Build a price_history list over consecutive fake past dates."""
    return [{"date": f"2026-08-{i+1:02d}", "price": p} for i, p in enumerate(prices)]


def run():
    # --- parsing -----------------------------------------------------------
    soup = BeautifulSoup(SAMPLE, "html.parser")
    items = checker.extract_items(soup, "https://www.amazon.com")
    assert set(items) == {"AAA111", "BBB222", "CCC333"}, items
    assert items["AAA111"]["price"] == 19.99
    assert items["AAA111"]["url"] == "https://www.amazon.com/dp/B00CABLE?ref=x"
    assert items["CCC333"]["price"] is None
    print("PASS: parsing")

    # --- day one: first sighting, no false deal ----------------------------
    history = {}
    fresh = {"K": {"title": "Keyboard", "url": "u", "price": 120.0, "image": None}}
    assert checker.evaluate(fresh, history, min_pct=5) == []
    assert history["K"]["price_history"][-1]["price"] == 120.0
    print("PASS: day one baseline, no false deal")

    # --- too little history: no deal even on a drop ------------------------
    # One seeded point + today's = 2 points, still below the 3-point minimum.
    history = {"K": {"title": "Keyboard", "url": "u", "price_history": hist([120])}}
    items = {"K": {"title": "Keyboard", "url": "u", "price": 99.0, "image": None}}
    assert checker.evaluate(items, history, min_pct=5) == [], "needs >=3 points"
    print("PASS: withholds alerts until enough history")

    # --- genuine drop vs typical price is detected -------------------------
    history = {"K": {"title": "Keyboard", "url": "u", "price_history": hist([120, 120, 120])}}
    items = {"K": {"title": "Keyboard", "url": "u", "price": 99.0, "image": None}}
    deals = checker.evaluate(items, history, min_pct=5)
    assert len(deals) == 1 and deals[0]["discount_pct"] == 17.5, deals
    print("PASS: detects a real drop vs the typical price")

    # --- REGRESSION: a temporary spike must NOT create a phantom deal ------
    # Mirrors the real YVE case: price spiked then returned to normal.
    history = {"Y": {"title": "Cat toy", "url": "u",
                     "price_history": hist([55.37, 54.10, 52.62, 58.23, 58.23])}}
    items = {"Y": {"title": "Cat toy", "url": "u", "price": 54.33, "image": None}}
    deals = checker.evaluate(items, history, min_pct=5)
    assert deals == [], f"spike then normal should NOT be a deal, got {deals}"
    print("PASS: temporary spike does not create a phantom discount")

    # --- no repeat alert for the same price --------------------------------
    history = {"K": {"title": "Keyboard", "url": "u", "last_notified_price": 99.0,
                     "price_history": hist([120, 120, 120, 99])}}
    items = {"K": {"title": "Keyboard", "url": "u", "price": 99.0, "image": None}}
    assert checker.evaluate(items, history, min_pct=5) == [], "no repeat at same price"
    print("PASS: no duplicate alert for the same price")

    print("\nAll logic tests passed.")


if __name__ == "__main__":
    run()
