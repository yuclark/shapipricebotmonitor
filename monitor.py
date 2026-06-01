import os
import time
import random
import sqlite3
import logging
from datetime import datetime
from curl_cffi import requests
from dotenv import load_dotenv

# Initialize local environment configurations
load_dotenv()

# ==============================================================================
# 1. CONFIGURATION
# ==============================================================================
SHOP_ID = 41735247               # Feralde Perfume Store ID
CHECK_INTERVAL_MINUTES = 15      # Frequency of store checking cycles

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

DB_FILE = "shopee_monitor.db"
BASE_URL = "https://shopee.ph"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)

if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
    logging.critical("CRITICAL: Telegram environment credentials missing from .env file!")
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
# 3. UTILITIES & GUEST HEADERS
# ==============================================================================
def get_guest_headers():
    return {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"{BASE_URL}/shop/{SHOP_ID}",
        "X-Requested-With": "XMLHttpRequest"
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
# 5. CORE MONITOR CYCLE (WITH AUTOMATED SESSION WARMING)
# ==============================================================================
def monitor_store_cycle(session):
    logging.info(f"Starting store scraping cycle for Shop ID: {SHOP_ID}")
    
    # 🌟 NEW: Session Warmup Handshake
    # If our session pool is empty, we visit the public store page HTML first to collect cookies naturally
    if not session.cookies:
        logging.info("Session cookies empty. Performing public landing page warm-up...")
        try:
            shop_front_url = f"{BASE_URL}/shop/{SHOP_ID}"
            warmup_response = session.get(shop_front_url, impersonate="chrome124", timeout=15)
            warmup_response.raise_for_status()
            logging.info("Successfully acquired anonymous guest tracking cookies.")
            time.sleep(random.randint(2, 4))
        except Exception as warmup_err:
            logging.error(f"Session onboarding warmup failed: {warmup_err}")
            # Continue anyway and attempt the API stream path
    
    limit = 30
    offset = 0
    has_more_items = True

    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()

        while has_more_items:
            api_url = (
                f"https://shopee.ph/api/v4/search/search_items?by=pop&limit={limit}"
                f"&match_id={SHOP_ID}&newest={offset}&order=desc&page_type=shop"
                f"&scenario=PAGE_SHOP&version=2"
            )

            try:
                response = session.get(
                    api_url, 
                    headers=get_guest_headers(), 
                    impersonate="chrome124", 
                    timeout=15
                )
                
                if response.status_code == 403:
                    logging.error("403 Forbidden: Guest handshake rejected by firewall layers.")
                    break
                
                response.raise_for_status()
                data = response.json()
                
                items = data.get("data", {}).get("items", [])
                if not items or items is None:
                    logging.info("No more catalog items discovered. Ending cycle.")
                    break

                for item in items:
                    ib = item.get("item_basic", item) if item.get("item_basic") else item
                    if ib.get("itemid"):
                        process_product(
                            cursor=cursor,
                            item_id=ib.get("itemid"),
                            name=ib.get("name"),
                            price=ib.get("price"),
                            stock=ib.get("stock")
                        )

                conn.commit()
                logging.info(f"Successfully processed items offset range: {offset} -> {offset + len(items)}")
                
                if len(items) < limit:
                    has_more_items = False
                else:
                    offset += limit
                    time.sleep(random.randint(4, 8))

            except Exception as req_err:
                logging.error(f"Error handling public search catalog stream: {req_err}")
                break  

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