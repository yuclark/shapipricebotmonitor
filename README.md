# Shopee Multi-Item Price Monitor

This bot watches a list of public Shopee product URLs and sends Telegram alerts when a price drops or a product comes back in stock.

## Files
- `monitor.py` - main bot
- `products.txt` - list of product URLs to track
- `shopee_monitor.db` - SQLite database created automatically
- `.env` - Telegram credentials

## Setup
1. Install requirements:
   `pip install -r requirements.txt`
2. Install browser:
   `python -m playwright install chromium`
3. Fill `.env` with:
   `TELEGRAM_BOT_TOKEN=...`
   `TELEGRAM_CHAT_ID=...`
4. Put product URLs in `products.txt`
5. Run:
   `python monitor.py`