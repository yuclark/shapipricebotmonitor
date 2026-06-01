import asyncio
from playwright.async_api import async_playwright

SHOP_ID = 41735247
BASE_URL = "https://shopee.ph"

async def inspect():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="en-US",
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()

        await page.goto(f"{BASE_URL}/shop/{SHOP_ID}", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(6)

        # Screenshot to see what the page looks like
        await page.screenshot(path="shop_page.png", full_page=False)
        print("Screenshot saved: shop_page.png")

        # Print ALL elements that contain tab-like text
        print("\n--- All elements with tab-related text ---")
        elements = await page.query_selector_all("div, a, span, button, li")
        for el in elements:
            try:
                text = (await el.inner_text()).strip()
                tag = await el.evaluate("el => el.tagName")
                class_name = await el.get_attribute("class") or ""
                if text and len(text) < 40 and any(k in text.lower() for k in ["product", "all", "item", "shop"]):
                    if any(k in class_name.lower() for k in ["tab", "nav", "menu", "category", "filter"]):
                        print(f"  <{tag.lower()} class='{class_name}'> → '{text}'")
            except Exception:
                pass

        # Also print the full HTML of anything with "tab" in its class
        print("\n--- Raw HTML of tab-related elements ---")
        tab_els = await page.query_selector_all("[class*='tab'], [class*='Tab']")
        for el in tab_els[:20]:
            try:
                html = await el.evaluate("el => el.outerHTML")
                if len(html) < 500:
                    print(html)
                    print("---")
            except Exception:
                pass

        await browser.close()
        print("\nDone.")

asyncio.run(inspect())