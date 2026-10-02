# Shopify carrier-rate monitor

Hourly synthetic checkout against the `vefd28-bb.myshopify.com` **development store**.
Each run (GitHub Actions, started every hour at :23 UTC by a Claude scheduled routine) does 20 checkouts (about 21 minutes):

1. open `/collections/all`, pick a random product, add it to the cart
2. open checkout, fill a test email and a Bangladesh test address
3. wait for shipping rates and check that **Free Shipping + $2.25 Shipping Protection / $2.25** is offered

It stops at the shipping-method stage. It never enters payment details, never places an order,
and never changes the store.

- Results: the run's **Summary** tab (table per checkout) and the `carrier-monitor-<id>` artifact
  (screenshots + `summary.json`). A failing check turns the run red and GitHub emails you.
- Manual run: Actions → *Shopify carrier-rate monitor* → **Run workflow** (choose the number of checkouts).
- Settings via env vars in `monitoring/shopify_carrier_monitor_cloud.py`: `EXPECTED_RATE_NAME`,
  `EXPECTED_RATE_AMOUNT`, `STEP_DELAY_SEC`, `DELAY_BETWEEN_RUNS_SEC`, `HEADLESS`.

