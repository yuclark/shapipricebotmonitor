import os
import time
import random
import sqlite3
import logging
from datetime import datetime
from curl_cffi import requests
# Injected dependency loader
from dotenv import load_dotenv

# Initialize tracking environment configuration parameters
load_dotenv()

# ==============================================================================
# 1. ENV CONFIGURATION STRINGS
# ==============================================================================
SHOP_ID = 41735247               # Feralde Perfume Store ID
CHECK_INTERVAL_MINUTES = 15      # Frequency of store checking cycles

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SHOPEE_COOKIE = os.getenv("SHOPEE_COOKIE")

DB_FILE = "shopee_monitor.db"
BASE_URL = "https://shopee.ph"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)

# Core sanity check to prevent empty runtimes if .env layout is unreadable
if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, SHOPEE_COOKIE]):
    logging.critical("CRITICAL: Environment configs could not load completely from .env file!")
    raise SystemExit(1)

# ==============================================================================
# 2. STATE MANAGEMENT
# ==============================================================================
def init_db():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS products (
                item_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                price INTEGER NOT NULL,
                stock INTEGER NOT NULL,
                last_checked TIMESTAMP NOT NULL
            )
        ''')
        conn.commit()
    logging.info("Database initialized successfully.")

# ==============================================================================
# 3. UTILITIES & HEADERS
# ==============================================================================
def get_authenticated_headers():
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "Cookie": SHOPEE_COOKIE,
        "X-CSRFToken": "brjV2HDKwsqzs0zgUxyQ8nYbQlrTzDAv",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{BASE_URL}/shop/{SHOP_ID}"
    }

def format_price(raw_price: int) -> str:
    return f"₱{raw_price / 100000:,.2f}"

# ==============================================================================
# 4. ALERTS
# ==============================================================================
def send_telegram_alert(alert_type: str, item_name: str, item_id: int, old_val: str, new_val: str):
    product_url = f"{BASE_URL}/product/{SHOP_ID}/{item_id}"
    clean_name = item_name.replace("<", "&lt;").replace(">", "&gt;")

    if alert_type == "PRICE_DROP":
        text = (
            f"🚨 <b>PRICE DROP ALERT!</b>\n\n"
            f"📦 <b>Product:</b> <a href='{product_url}'>{clean_name}</a>\n"
            f"❌ <b>Old Price:</b> {old_val}\n"
            f"✅ <b>New Price:</b> <b>{new_val}</b>"
        )
    elif alert_type == "RESTOCK":
        text = (
            f"🔥 <b>RESTOCK ALERT!</b>\n\n"
            f"📦 <b>Product:</b> <a href='{product_url}'>{clean_name}</a>\n"
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
        "disable_web_page_preview": False
    }

    try:
        requests.post(telegram_url, json=payload, timeout=10)
    except Exception as e:
        logging.error(f"Failed to push message payload to Telegram: {e}")

def process_product(cursor, item_id: int, name: str, price: int, stock: int):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("SELECT price, stock FROM products WHERE item_id = ?", (item_id,))
    row = cursor.fetchone()

    if row is None:
        cursor.execute(
            "INSERT INTO products (item_id, name, price, stock, last_checked) VALUES (?, ?, ?, ?, ?)",
            (item_id, name, price, stock, now)
        )
        logging.info(f"New product tracked: {name[:30]}...")
    else:
        old_price, old_stock = row
        if price < old_price:
            logging.info(f"Price Drop: {name}")
            send_telegram_alert("PRICE_DROP", name, item_id, format_price(old_price), format_price(price))

        if old_stock == 0 and stock > 0:
            logging.info(f"Restock: {name}")
            send_telegram_alert("RESTOCK", name, item_id, "Out of Stock", str(stock))

        cursor.execute(
            "UPDATE products SET name = ?, price = ?, stock = ?, last_checked = ? WHERE item_id = ?",
            (name, price, stock, now, item_id)
        )

# ==============================================================================
# 5. CORE MONITOR CYCLE
# ==============================================================================
def monitor_store_cycle(session):
    logging.info(f"Starting store scraping cycle for Shop ID: {SHOP_ID}")
    
    api_url = f"https://shopee.ph/api/v4/shop/get_shop_tab"
    json_payload = {"shopid": SHOP_ID, "tab_type": 0, "limit": 30, "offset": 0}

    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()

        try:
            response = session.post(
                api_url, 
                headers=get_authenticated_headers(), 
                json=json_payload,
                impersonate="chrome124", 
                timeout=15
            )
            
            if response.status_code == 403:
                logging.error("403 Forbidden: Cookie missing, invalid, or expired.")
                return
            
            response.raise_for_status()
            data = response.json()
            
            sections = data.get("data", {}).get("modules", [])
            items_found = False

            for module in sections:
                if module.get("type") == "product_list" or "items" in module.get("data", {}):
                    items = module.get("data", {}).get("items", [])
                    if items:
                        items_found = True
                        for item in items:
                            process_product(
                                cursor=cursor,
                                item_id=item.get("itemid"),
                                name=item.get("name"),
                                price=item.get("price"),
                                stock=item.get("stock")
                            )
            
            if items_found:
                conn.commit()
                logging.info("Successfully synchronized storefront catalog items.")
            else:
                logging.warning("No clear product modules mapped in this shop configuration tab layout.")

        except Exception as req_err:
            logging.error(f"Error handling API storefront data profile stream: {req_err}")

# ==============================================================================
# 6. ENTRYPOINT
# ==============================================================================
if __name__ == "__main__":
    init_db()
    logging.info("Bot monitoring daemon initialized. Entering structural check timeline.")
    
    session_pool = requests.Session()
    
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", 
            json={"chat_id": TELEGRAM_CHAT_ID, "text": "🚀 <b>Shopee Monitor Bot is online and tracking Feralde!</b>", "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        logging.error(f"Could not send startup Telegram ping: {e}")
    
    while True:
        try:
            monitor_store_cycle(session_pool)
        except Exception as global_err:
            logging.critical(f"Unhandled critical loop error: {global_err}")
        
        logging.info(f"Cycle completed. Sleeping for {CHECK_INTERVAL_MINUTES} minutes...")
        time.sleep(CHECK_INTERVAL_MINUTES * 60)