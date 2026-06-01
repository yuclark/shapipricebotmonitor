import os
import re
import time
import random
import sqlite3
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

from curl_cffi import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

load_dotenv()

CHECK_INTERVAL_MINUTES = 15
DB_FILE = "shopee_monitor.db"
PRODUCTS_FILE = "products.txt"
BASE_URL = "https://shopee.ph"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)

if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
    logging.critical("CRITICAL: Telegram environment credentials missing from .env file!")
    raise SystemExit(1)

@dataclass
class TrackedProduct:
    item_id: int
    shop_id: int
    name: str
    url: str


def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS products (
                item_id INTEGER PRIMARY KEY,
                shop_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                price INTEGER NOT NULL,
                stock INTEGER NOT NULL,
                last_checked TIMESTAMP NOT NULL
            )
        """)
        conn.commit()
    logging.info("Database initialized successfully.")


def load_products_file(path: str = PRODUCTS_FILE) -> List[TrackedProduct]:
    if not os.path.exists(path):
        raise FileNotFoundError(f"Missing {path}. Put one Shopee product URL per line.")

    products: List[TrackedProduct] = []
    seen = set()
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            url = raw.strip()
            if not url or url.startswith("#"):
                continue
            parsed = parse_shopee_product_url(url)
            if not parsed:
                logging.warning(f"Skipping unsupported URL: {url}")
                continue
            key = (parsed.shop_id, parsed.item_id)
            if key in seen:
                continue
            seen.add(key)
            products.append(parsed)
    return products


def parse_shopee_product_url(url: str) -> Optional[TrackedProduct]:
    m = re.search(r'/product/(\d+)/(\d+)', url)
    if m:
        shop_id = int(m.group(1))
        item_id = int(m.group(2))
        return TrackedProduct(item_id=item_id, shop_id=shop_id, name=f"item_{item_id}", url=url)

    m = re.search(r'-i\.(\d+)\.(\d+)', url)
    if m:
        shop_id = int(m.group(1))
        item_id = int(m.group(2))
        slug = url.split("shopee.ph/")[-1].split("-i.")[0].replace("-", " ").strip()
        name = slug or f"item_{item_id}"
        return TrackedProduct(item_id=item_id, shop_id=shop_id, name=name, url=url)

    return None


def format_price(raw_price: int) -> str:
    return f"₱{raw_price / 100000:,.2f}"


def send_telegram_alert(alert_type: str, item_name: str, item_url: str, old_val: str, new_val: str):
    clean_name = item_name.replace("<", "&lt;").replace(">", "&gt;")
    if alert_type == "PRICE_DROP":
        text = (
            f"🚨 <b>PRICE DROP ALERT!</b>\n\n"
            f"📦 <b>Product:</b> <a href='{item_url}'>{clean_name}</a>\n"
            f"❌ <b>Old Price:</b> {old_val}\n"
            f"✅ <b>New Price:</b> <b>{new_val}</b>"
        )
    elif alert_type == "RESTOCK":
        text = (
            f"🔥 <b>RESTOCK ALERT!</b>\n\n"
            f"📦 <b>Product:</b> <a href='{item_url}'>{clean_name}</a>\n"
            f"❌ <b>Previous Status:</b> <del>{old_val}</del>\n"
            f"✅ <b>Current Stock:</b> <b>{new_val} Units available</b>"
        )
    else:
        return

    telegram_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        requests.post(telegram_url, json=payload, timeout=10)
    except Exception as e:
        logging.error(f"Failed to send Telegram alert: {e}")


def extract_price_stock(page) -> Optional[Tuple[int, int]]:
    price_text = None
    selectors = [
        'meta[property="product:price:amount"]',
        '[data-testid*="price"]',
        'div:has-text("₱")',
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            price_text = loc.inner_text(timeout=3000)
            if price_text:
                break
        except Exception:
            continue

    if not price_text:
        return None

    digits = re.sub(r"[^\d]", "", price_text)
    if not digits:
        return None
    price = int(digits)

    stock = 1
    try:
        body = page.content().lower()
        if any(x in body for x in ["out of stock", "sold out", "unavailable"]):
            stock = 0
    except Exception:
        pass

    return price, stock


def process_product(cursor, product: TrackedProduct, price: int, stock: int):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("SELECT price, stock FROM products WHERE item_id = ?", (product.item_id,))
    row = cursor.fetchone()

    if row is None:
        cursor.execute(
            "INSERT INTO products (item_id, shop_id, name, url, price, stock, last_checked) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (product.item_id, product.shop_id, product.name, product.url, price, stock, now)
        )
        logging.info(f"  [NEW] {product.name[:60]} — {format_price(price)}")
        send_telegram_alert("PRICE_DROP", product.name, product.url, "N/A", format_price(price))
        return

    old_price, old_stock = row
    price_diff = old_price - price
    if price_diff > 0:
        status_text = f"price dropped by {format_price(price_diff)}"
    elif price_diff < 0:
        status_text = f"price increased by {format_price(abs(price_diff))}"
    else:
        status_text = "no change"

    send_telegram_alert(
        "PRICE_DROP",
        product.name,
        product.url,
        f"Previous: {format_price(old_price)} ({status_text})",
        f"Current: {format_price(price)}"
    )

    if old_stock == 0 and stock > 0:
        send_telegram_alert("RESTOCK", product.name, product.url, "Out of Stock", str(stock))

    cursor.execute(
        "UPDATE products SET name = ?, url = ?, price = ?, stock = ?, last_checked = ? WHERE item_id = ?",
        (product.name, product.url, price, stock, now, product.item_id)
    )


def check_product(page, product: TrackedProduct) -> Optional[Tuple[int, int]]:
    try:
        page.goto(product.url, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except PlaywrightTimeoutError:
            pass
        time.sleep(random.uniform(2.0, 4.0))
        return extract_price_stock(page)
    except Exception as e:
        logging.error(f"Failed checking {product.item_id}: {e}")
        return None


def monitor_cycle(products: List[TrackedProduct]):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="en-US",
            viewport={"width": 1280, "height": 800},
        )
        page = context.new_page()

        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            for idx, product in enumerate(products, start=1):
                logging.info(f"Checking {idx}/{len(products)}: {product.name[:60]}")
                res = check_product(page, product)
                if not res:
                    continue
                price, stock = res
                process_product(cursor, product, price, stock)
                conn.commit()
                time.sleep(random.uniform(2.0, 4.0))

        browser.close()


def ensure_products_file(path: str = PRODUCTS_FILE):
    if os.path.exists(path):
        return
    with open(path, "w", encoding="utf-8") as f:
        f.write("# One Shopee product URL per line\n")
        f.write("# Example:\n")
        f.write("# https://shopee.ph/product-name-i.41735247.1234567890\n")


if __name__ == "__main__":
    ensure_products_file()
    init_db()
    products = load_products_file()
    if not products:
        logging.critical(f"No valid URLs found in {PRODUCTS_FILE}")
        raise SystemExit(1)

    logging.info("Bot monitoring daemon initialized.")
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": f"🚀 <b>Shopee Multi-Item Monitor is online!</b> Tracking {len(products)} items.",
                "parse_mode": "HTML"
            },
            timeout=10
        )
    except Exception as e:
        logging.error(f"Could not send startup Telegram ping: {e}")

    while True:
        try:
            monitor_cycle(products)
        except Exception as global_err:
            logging.critical(f"Unhandled critical loop error: {global_err}")
        logging.info(f"Cycle complete. Sleeping for {CHECK_INTERVAL_MINUTES} minutes...")
        time.sleep(CHECK_INTERVAL_MINUTES * 60)