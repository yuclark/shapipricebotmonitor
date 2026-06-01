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
def get_guest_headers(session):
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"{BASE_URL}/shop/{SHOP_ID}",
        "X-Requested-With": "XMLHttpRequest",
        "X-API-Source": "pc"
    }
    
    csrf_token = session.cookies.get("csrftoken")
    if csrf_token:
        headers["X-CSRFToken"] = csrf_token
        
    return headers

def format_price(raw_price: int) -> str:
    return f"₱{raw_price / 100000:,.2f}"Prefix

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
# 5. CORE MONITOR CYCLE (PUBLIC STOREFRONT TABS ROUTE)
# ==============================================================================
def monitor_store_cycle(session):
    logging.info(f"Starting store scraping cycle for Shop ID: {SHOP_ID}")
    
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
    
    limit = 30
    offset = 0
    has_more_items = True

    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()

        while has_more_items:
            # PIVOT: Querying the web crawler-accessible GET layout endpoint 
            api_url = f"{BASE_URL}/api/v4/shop/get_shop_tab?limit={limit}&offset={offset}&shopid={SHOP_ID}&tab_type=0"

            try:
                response = session.get(
                    api_url, 
                    headers=get_guest_headers(session), 
                    impersonate="chrome124", 
                    timeout=15
                )
                
                if response.status_code == 403:
                    logging.error("403 Forbidden: Public storefront route rejected by firewall layers.")
                    break
                
                response.raise_for_status()
                data = response.json()
                
                modules = data.get("data", {}).get("modules", [])
                if not modules:
                    logging.info("No active display modules discovered on storefront. Ending cycle.")
                    break

                items_processed_in_page = 0
                for module in modules:
                    # Look inside both custom product grids and generalized carousel widgets
                    module_data = module.get("data", {})
                    items = module_data.get("items", []) if isinstance(module_data, dict) else []
                    
                    if items:
                        for item in items:
                            # Re-map structurally nested product dictionary references safely
                            ib = item.get("item_basic", item) if item.get("item_basic") else item
                            if ib.get("itemid"):
                                process_product(
                                    cursor=cursor,
                                    item_id=ib.get("itemid"),
                                    name=ib.get("name"),
                                    price=ib.get("price"),
                                    stock=ib.get("stock")
                                )
                                items_processed_in_page += 1

                conn.commit()
                logging.info(f"Successfully processed items range offset: {offset} -> {offset + items_processed_in_page}")
                
                # If no products were returned inside the modules, we have reached the end of the collection catalog
                if items_processed_in_page == 0:
                    has_more_items = False
                else:
                    offset += limit
                    time.sleep(random.randint(4, 8))

            except Exception as req_err:
                logging.error(f"Error handling public storefront catalog stream: {req_err}")
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