"""Offline smoke test for parsing + discount logic (no network, no email)."""
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


def run():
    soup = BeautifulSoup(SAMPLE, "html.parser")
    items = checker.extract_items(soup, "https://www.amazon.com")
    assert set(items) == {"AAA111", "BBB222", "CCC333"}, items
    assert items["AAA111"]["price"] == 19.99
    assert items["AAA111"]["url"] == "https://www.amazon.com/dp/B00CABLE?ref=x"
    assert items["CCC333"]["price"] is None
    print("PASS: parsing")

    # Day 1: establish baseline prices (no deals, first sighting).
    history = {}
    deals = checker.evaluate(items, history, min_pct=5)
    assert deals == [], deals
    assert history["BBB222"]["reference_price"] == 120.00
    print("PASS: day 1 baseline, no false deals")

    # Day 2: keyboard drops to $99 (-17.5%) -> should notify. Cable steady.
    items2 = {
        "AAA111": {"title": "USB-C Cable 6ft", "url": "u", "price": 19.99},
        "BBB222": {"title": "Mechanical Keyboard", "url": "u", "price": 99.00},
    }
    deals = checker.evaluate(items2, history, min_pct=5)
    assert len(deals) == 1 and deals[0]["title"] == "Mechanical Keyboard", deals
    assert deals[0]["discount_pct"] == 17.5, deals
    print("PASS: day 2 detects the price drop")

    # Day 3: same $99 price -> no repeat notification.
    deals = checker.evaluate(items2, history, min_pct=5)
    assert deals == [], deals
    print("PASS: day 3 no duplicate alert for the same price")

    # Day 4: drops further to $89 -> new low -> notify again.
    items4 = {"BBB222": {"title": "Mechanical Keyboard", "url": "u", "price": 89.00}}
    deals = checker.evaluate(items4, history, min_pct=5)
    assert len(deals) == 1, deals
    print("PASS: day 4 re-alerts on a new lower low")

    print("\nAll logic tests passed.")


if __name__ == "__main__":
    run()
