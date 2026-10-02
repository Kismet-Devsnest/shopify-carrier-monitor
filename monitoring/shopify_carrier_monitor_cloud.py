"""Shopify carrier-rate synthetic monitor (cloud scheduled-task edition).

Walks the storefront checkout up to the shipping-method stage and verifies
the carrier service rate. It NEVER enters payment details, never clicks a
pay / complete-order button and never modifies the store.
"""
import base64
import hashlib
import json
import os
import random
import subprocess
import time
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

# Cloud-monitor configuration.
# Keep this small per scheduled run; let Claude's scheduler invoke it again.
NUM_RUNS = int(os.getenv("NUM_RUNS", "3"))
STEP_DELAY_SEC = float(os.getenv("STEP_DELAY_SEC", "7"))
DELAY_BETWEEN_RUNS_SEC = float(os.getenv("DELAY_BETWEEN_RUNS_SEC", "10"))
# Visible (headed) browser by default, like the locally working script.
# run_monitor.sh provides a virtual display (Xvfb) in the cloud.
HEADLESS = os.getenv("HEADLESS", "0") == "1"

STORE_BASE = os.getenv("STORE_BASE", "https://vefd28-bb.myshopify.com").rstrip("/")
if not STORE_BASE.startswith(("http://", "https://")):
    STORE_BASE = f"https://{STORE_BASE}"
COLLECTION_BASE = f"{STORE_BASE}/collections/all"
COLLECTION_MAX_PAGE = int(os.getenv("COLLECTION_MAX_PAGE", "5"))

# Storefront password of the development store (Online Store > Preferences).
# Supplied via the cloud environment's variables; never committed.
STOREFRONT_PASSWORD = os.getenv("SHOPIFY_STOREFRONT_PASSWORD", "")

# Set these to the exact service/rate your Lambda is expected to return.
EXPECTED_RATE_NAME = os.getenv("EXPECTED_RATE_NAME", "Free Shipping + $2.25 Shipping Protection")
EXPECTED_RATE_AMOUNT = os.getenv("EXPECTED_RATE_AMOUNT", "$2.25")
EXPECTED_CURRENCY = os.getenv("EXPECTED_CURRENCY", "USD")

ARTIFACT_DIR = Path(os.getenv("ARTIFACT_DIR", "artifacts"))
ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)

CONTACTS_FILE = Path(
    os.getenv("CONTACTS_FILE", Path(__file__).with_name("test_contacts.json"))
)
CONTACTS = json.loads(CONTACTS_FILE.read_text())

# The cloud sandbox re-terminates TLS with its own CA. Trust exactly that CA
# (by public-key pin) instead of disabling certificate checks.
PROXY_CA = Path(os.getenv("PROXY_CA_CERT", "/root/.ccr/agent-proxy-ca.crt"))


def chromium_args() -> list[str]:
    try:
        if not PROXY_CA.is_file():
            return []
    except OSError:  # e.g. /root unreadable on GitHub Actions runners
        return []
    pub = subprocess.run(
        ["openssl", "x509", "-in", str(PROXY_CA), "-pubkey", "-noout"],
        check=True, capture_output=True,
    ).stdout
    der = subprocess.run(
        ["openssl", "pkey", "-pubin", "-outform", "der"],
        input=pub, check=True, capture_output=True,
    ).stdout
    spki = base64.b64encode(hashlib.sha256(der).digest()).decode()
    return [f"--ignore-certificate-errors-spki-list={spki}"]


def unlock_storefront(page) -> None:
    """Pass the dev-store password page if Shopify redirected to it."""
    if "/password" not in urlparse(page.url).path:
        return
    if not STOREFRONT_PASSWORD:
        raise AssertionError(
            "Storefront is password-protected and SHOPIFY_STOREFRONT_PASSWORD is not set."
        )
    pw = page.locator("input[type='password']").first
    if not pw.is_visible():
        # Newer themes hide the field behind an "Enter using password" link.
        try:
            page.get_by_text("Enter using password").first.click(timeout=5000)
        except Exception:
            pass
    pw.wait_for(state="visible", timeout=10000)
    pw.fill(STOREFRONT_PASSWORD)
    pw.press("Enter")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(1000)
    if "/password" in urlparse(page.url).path:
        raise AssertionError("Storefront password was rejected.")


def fill_once(page, selectors: list[str], value: str) -> None:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=3000)
            loc.fill(value)
            return
        except Exception:
            continue
    raise AssertionError(f"Could not fill; none matched: {selectors}")


def wait_for_shipping_rates_on_page(page, timeout_ms: int = 35000) -> None:
    try:
        page.locator(
            "input[name='postalCode'], input[autocomplete='shipping postal-code']"
        ).first.press("Tab")
    except Exception:
        pass

    page.wait_for_timeout(500)
    page.wait_for_selector(
        "input[type='radio']:visible, [role='radio']:visible",
        timeout=timeout_ms,
    )
    # Shopify first renders skeleton radios while the carrier service
    # responds; wait until at least one option has a real label.
    page.wait_for_function(
        """() => [...document.querySelectorAll("input[type='radio']")].some(el => {
            const lb = (el.labels && el.labels[0]) || el.closest('label');
            return lb && lb.offsetParent && lb.innerText.trim().length > 0;
        })""",
        timeout=timeout_ms,
    )


def collect_visible_shipping_labels(page) -> list[str]:
    lines = []

    try:
        radios = page.locator("input[type='radio']:visible")
        n = min(radios.count(), 8)

        for i in range(n):
            try:
                t = radios.nth(i).evaluate(
                    """el => {
                        if (el.labels && el.labels.length)
                            return el.labels[0].innerText.trim();
                        const lb = el.closest('label');
                        return lb ? lb.innerText.trim() : '';
                    }"""
                )
                t = (t or "").strip()
                if t and t not in lines:
                    lines.append(t)
            except Exception:
                pass

        roles = page.locator("[role='radio']:visible")
        m = min(roles.count(), 8)

        for i in range(m):
            try:
                t = roles.nth(i).inner_text(timeout=1500).strip()
                if t and t not in lines:
                    lines.append(t)
            except Exception:
                pass

    except Exception:
        pass

    return lines


def verify_expected_rate(page, labels: list[str], result: dict) -> None:
    body = page.locator("body").inner_text().strip()

    result["carrier_found"] = EXPECTED_RATE_NAME.lower() in body.lower()
    result["amount_found"] = EXPECTED_RATE_AMOUNT in body

    if not labels:
        raise AssertionError("No visible shipping options were detected.")

    if not result["carrier_found"]:
        raise AssertionError(
            f"Expected carrier/service '{EXPECTED_RATE_NAME}' was not found. "
            f"Visible rates: {labels}"
        )

    if not result["amount_found"]:
        raise AssertionError(
            f"Expected rate '{EXPECTED_RATE_AMOUNT}' was not found. "
            f"Visible rates: {labels}"
        )


def run_once(browser, run_number: int) -> dict:
    contact = random.choice(CONTACTS)
    page = None
    result = {
        "run": run_number,
        "status": "FAIL",
        "stage": "open_collection",
        "product": None,
        "country": contact["country_code"],
        "address": f"{contact['address']}, {contact['city']}, "
                   f"{contact.get('state', '')} {contact['zip']}, {contact['country_code']}",
        "shipping_options": [],
        "carrier_found": False,
        "amount_found": False,
        "error": None,
        "screenshot": None,
    }

    try:
        page = browser.new_page()

        # 1. Random product from the collection.
        page_num = random.randint(1, COLLECTION_MAX_PAGE)
        page.goto(
            f"{COLLECTION_BASE}?page={page_num}",
            wait_until="domcontentloaded",
        )
        unlock_storefront(page)
        if "/collections/all" not in page.url:
            page.goto(
                f"{COLLECTION_BASE}?page={page_num}",
                wait_until="domcontentloaded",
            )
        page.wait_for_selector(
            "a[href*='/products/']",
            state="attached",
            timeout=15000,
        )
        page.wait_for_timeout(1000)

        hrefs = page.locator(
            "a[href*='/products/']"
        ).evaluate_all("els => els.map(el => el.href)")

        unique_products = list(dict.fromkeys(
            u.split("?")[0]
            for u in hrefs
            if u and "/products/" in u
        ))

        if not unique_products:
            raise AssertionError("No products found in the collection.")

        product_url = random.choice(unique_products)
        result["product"] = product_url

        print(
            f"[Run {run_number}/{NUM_RUNS}] "
            f"Page {page_num} -> {product_url}"
        )

        time.sleep(STEP_DELAY_SEC)

        # 2. Product.
        result["stage"] = "open_product"
        page.goto(product_url, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        time.sleep(STEP_DELAY_SEC)

        # 3. Add to cart.
        result["stage"] = "add_to_cart"
        page.get_by_test_id("standalone-add-to-cart").click()
        page.wait_for_timeout(1500)
        time.sleep(STEP_DELAY_SEC)

        # 4. Checkout.
        result["stage"] = "open_checkout"
        page.goto(f"{STORE_BASE}/checkout", wait_until="domcontentloaded")
        time.sleep(STEP_DELAY_SEC)

        page.wait_for_selector(
            "input[name='email'], input#email, "
            "input[name='checkout[email_or_phone]']",
            state="visible",
            timeout=20000,
        )
        time.sleep(STEP_DELAY_SEC)

        # 5. Contact email.
        result["stage"] = "fill_email"
        fill_once(
            page,
            [
                "input[name='email']",
                "input#email",
                "input[name='checkout[email_or_phone]']",
            ],
            contact["email"],
        )

        page.wait_for_timeout(300)

        # 6. Country.
        result["stage"] = "select_country"
        cc = contact["country_code"]

        try:
            page.select_option("select[name='countryCode']", value=cc)
        except Exception:
            try:
                page.select_option(
                    "select[name='checkout[shipping_address][country]']",
                    label=cc,
                )
            except Exception:
                page.select_option(
                    "select#checkout_shipping_address_country",
                    label=cc,
                )

        page.wait_for_timeout(500)

        # 7. State/province where applicable.
        state = contact.get("state", "")
        if state:
            try:
                page.select_option("select[name='zone']", value=state)
            except Exception:
                try:
                    page.select_option(
                        "select[name='checkout[shipping_address][province]']",
                        value=state,
                    )
                except Exception:
                    try:
                        fill_once(
                            page,
                            [
                                "input[name='zone']",
                                "input[name='checkout[shipping_address][province]']",
                                "input[autocomplete='address-level1']",
                            ],
                            state,
                        )
                    except Exception:
                        pass

        # 8. Shipping address.
        result["stage"] = "fill_address"
        fill_once(
            page,
            [
                "input[name='firstName']",
                "input[autocomplete='shipping given-name']",
                "input[name='checkout[shipping_address][first_name]']",
            ],
            contact["first"],
        )

        fill_once(
            page,
            [
                "input[name='lastName']",
                "input[autocomplete='shipping family-name']",
                "input[name='checkout[shipping_address][last_name]']",
            ],
            contact["last"],
        )

        fill_once(
            page,
            [
                "input[name='address1']",
                "input[autocomplete='shipping address-line1']",
                "input[name='checkout[shipping_address][address1]']",
            ],
            contact["address"],
        )

        fill_once(
            page,
            [
                "input[name='city']",
                "input[autocomplete='shipping address-level2']",
                "input[name='checkout[shipping_address][city]']",
            ],
            contact["city"],
        )

        fill_once(
            page,
            [
                "input[name='postalCode']",
                "input[autocomplete='shipping postal-code']",
                "input[name='checkout[shipping_address][zip]']",
            ],
            contact["zip"],
        )

        time.sleep(STEP_DELAY_SEC)

        # 9. Wait for shipping rates.
        result["stage"] = "wait_for_shipping_rates"
        wait_for_shipping_rates_on_page(page)

        page.wait_for_timeout(1500)

        labels = collect_visible_shipping_labels(page)
        result["shipping_options"] = labels

        for label in labels:
            print(f"  Rate: {label[:300]}")

        # 10. Actual monitoring assertion. Stop here: shipping-method stage.
        result["stage"] = "verify_expected_rate"
        verify_expected_rate(page, labels, result)

        screenshot = ARTIFACT_DIR / f"pass_{run_number}.png"
        page.screenshot(path=str(screenshot), full_page=True)
        result["screenshot"] = str(screenshot)
        result["status"] = "PASS"

        print(
            f"  PASS: {EXPECTED_RATE_NAME} / "
            f"{EXPECTED_RATE_AMOUNT} {EXPECTED_CURRENCY}"
        )

    except Exception as exc:
        result["error"] = str(exc).splitlines()[0][:500]
        screenshot = ARTIFACT_DIR / f"FAIL_{run_number}.png"
        if page:
            try:
                if not result["shipping_options"]:
                    result["shipping_options"] = collect_visible_shipping_labels(page)
                page.screenshot(path=str(screenshot), full_page=True)
                result["screenshot"] = str(screenshot)
            except Exception:
                pass

    finally:
        if page:
            try:
                page.close()
            except Exception:
                pass

    return result


def main() -> int:
    started = datetime.now(timezone.utc).isoformat()
    print(f"SHOPIFY CARRIER MONITOR START {started}")
    print(f"Store: {STORE_BASE}")
    print(f"Runs: {NUM_RUNS} (headless={HEADLESS}, step delay={STEP_DELAY_SEC}s)")
    print(
        f"Expected: {EXPECTED_RATE_NAME} / "
        f"{EXPECTED_RATE_AMOUNT} {EXPECTED_CURRENCY}"
    )

    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS, args=chromium_args())

        try:
            for run in range(1, NUM_RUNS + 1):
                result = run_once(browser, run)
                results.append(result)
                if result["status"] == "FAIL":
                    print(
                        f"  FAIL run {run} at stage '{result['stage']}': "
                        f"{result['error']}"
                    )

                if run < NUM_RUNS:
                    time.sleep(DELAY_BETWEEN_RUNS_SEC)
        finally:
            browser.close()

    failures = sum(r["status"] == "FAIL" for r in results)
    summary = {
        "timestamp": started,
        "store": STORE_BASE,
        "runs": NUM_RUNS,
        "passed": NUM_RUNS - failures,
        "failed": failures,
        "expected_carrier": EXPECTED_RATE_NAME,
        "expected_rate": f"{EXPECTED_RATE_AMOUNT} {EXPECTED_CURRENCY}",
        "result": "FAIL" if failures else "PASS",
        "details": results,
    }
    summary_path = ARTIFACT_DIR / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))
    print("SUMMARY_JSON " + json.dumps(summary))

    if failures:
        print(
            f"RESULT: FAIL — {failures}/{NUM_RUNS} run(s) failed"
        )
        return 1

    print(f"RESULT: PASS — {NUM_RUNS}/{NUM_RUNS} run(s) passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
