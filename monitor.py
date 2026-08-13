#!/usr/bin/env python3
"""
Hermès Japan Stock Monitor → Telegram Bot
Uses Playwright (headless Chromium) to bypass Cloudflare bot detection.

Required env vars:
  TELEGRAM_TOKEN   - Bot token from @BotFather
  TELEGRAM_CHAT_ID - Target channel / user / group ID
"""

import json
import os
import re
import time
import sys
from datetime import datetime, timezone

import requests

# ── Config ─────────────────────────────────────────────────────────────────────

TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

STATE_FILE   = "state.json"
PRODUCT_FILE = "products.json"
REQUEST_DELAY = 2   # seconds between product checks
PAGE_TIMEOUT  = 30_000  # ms for Playwright page load
JS_WAIT       = 3_000   # ms to wait for JS to render after DOMContentLoaded

# ── Browser fetch (Playwright) ──────────────────────────────────────────────────

_browser = None  # shared Chromium instance for the run


def fetch_html(url: str) -> str | None:
    global _browser
    try:
        ctx = _browser.new_context(
            locale="ja-JP",
            user_agent=(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/17.0 Mobile/15E148 Safari/604.1"
            ),
            extra_http_headers={
                "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Referer": "https://www.hermes.com/jp/ja/",
            },
        )
        page = ctx.new_page()
        resp = page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        if resp and resp.status != 200:
            print(f"  HTTP {resp.status}", end=" ", flush=True)
            ctx.close()
            return None
        page.wait_for_timeout(JS_WAIT)  # let Next.js hydrate and render stock status
        html = page.content()
        ctx.close()
        return html
    except Exception as e:
        print(f"  fetch error: {e}", end=" ", flush=True)
        return None


def extract_og_image(html: str) -> str | None:
    patterns = [
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def _check_next_data(html: str) -> bool | None:
    """Parse Next.js __NEXT_DATA__ JSON. Returns True/False/None."""
    m = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html, re.DOTALL)
    if not m:
        return None
    try:
        text = m.group(1)
    except Exception:
        return None

    out_signals = ["OutOfStock", "out_of_stock", "outOfStock"]
    for s in out_signals:
        if s in text:
            return False

    in_signals = ["InStock", "in_stock", "inStock", "カートに入れる", "addToCart"]
    for s in in_signals:
        if s in text:
            return True

    return None


def is_in_stock(html: str) -> bool:
    next_result = _check_next_data(html)
    if next_result is not None:
        return next_result

    out_signals = [
        "OutOfStock",
        '"availability":"http://schema.org/OutOfStock"',
        '"availability": "http://schema.org/OutOfStock"',
        "product-not-available",
        "sold-out",
        "在庫なし",
    ]
    for s in out_signals:
        if s.lower() in html.lower():
            return False

    in_signals = [
        '"availability":"http://schema.org/InStock"',
        '"availability": "http://schema.org/InStock"',
        '"availability":"InStock"',
        "カートに入れる",
        "add-to-cart-button",
    ]
    return any(s in html for s in in_signals)


# ── Telegram ────────────────────────────────────────────────────────────────────

TG_BASE = f"https://api.telegram.org/bot{TOKEN}"


def _jst_now() -> str:
    from datetime import timedelta
    jst = timezone(timedelta(hours=9))
    return datetime.now(jst).strftime("%Y/%m/%d %H:%M JST")


def _build_caption(p: dict) -> str:
    price = f"¥{p['price']:,}"
    return (
        f"🛍 <b>入荷のお知らせ</b>\n\n"
        f"<b>{p['name']}</b>\n"
        f"━━━━━━━━━━━━\n"
        f"📏 サイズ：<code>{p['size']}</code>\n"
        f"🎨 カラー：<code>{p['color']}</code>\n"
        f"💴 価格　：<code>{price}</code>\n"
        f"⏰ 確認　：<code>{_jst_now()}</code>"
    )


def _build_markup(url: str) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "🔗 公式サイトで確認する", "url": url}
        ]]
    }


def send_notification(product: dict, image_url: str | None) -> bool:
    caption = _build_caption(product)
    markup  = _build_markup(product["url"])

    if image_url:
        r = requests.post(f"{TG_BASE}/sendPhoto", json={
            "chat_id":      CHAT_ID,
            "photo":        image_url,
            "caption":      caption,
            "parse_mode":   "HTML",
            "reply_markup": markup,
        }, timeout=20)
        if r.ok:
            print(f"  → Telegram photo sent ✓")
            return True
        print(f"  → sendPhoto failed ({r.status_code}), falling back to text")

    r = requests.post(f"{TG_BASE}/sendMessage", json={
        "chat_id":                  CHAT_ID,
        "text":                     caption,
        "parse_mode":               "HTML",
        "reply_markup":             markup,
        "disable_web_page_preview": False,
    }, timeout=20)

    if r.ok:
        print(f"  → Telegram message sent ✓")
        return True

    print(f"  → Telegram FAILED: {r.text}")
    return False


def send_test_message() -> None:
    r = requests.post(f"{TG_BASE}/sendMessage", json={
        "chat_id":    CHAT_ID,
        "text":       "✅ <b>HWatch Bot 接続確認</b>\n\nエルメス在庫監視ボットが正常に動作しています。",
        "parse_mode": "HTML",
    }, timeout=20)
    if r.ok:
        print("Test message sent successfully!")
    else:
        print(f"Test message failed: {r.text}")
        sys.exit(1)


# ── State ────────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ── Main ─────────────────────────────────────────────────────────────────────────

def main():
    global _browser

    if not TOKEN or not CHAT_ID:
        print("ERROR: TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set.")
        sys.exit(1)

    if "--test" in sys.argv:
        send_test_message()
        return

    with open(PRODUCT_FILE, encoding="utf-8") as f:
        products = json.load(f)

    tracked = [p for p in products if p.get("tracked", True)]
    print(f"Monitoring {len(tracked)} products at {_jst_now()}")
    print("=" * 48)

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        _browser = pw.chromium.launch(headless=True)

        state   = load_state()
        changed = False

        for i, p in enumerate(tracked):
            print(f"[{i+1}/{len(tracked)}] {p['name']} {p['color']} …", end=" ", flush=True)

            html = fetch_html(p["url"])
            if html is None:
                print("SKIP")
                continue

            available     = is_in_stock(html)
            prev          = state.get(p["id"], {})
            was_available = prev.get("available", False)

            print("IN STOCK ✓" if available else "out of stock")

            state.setdefault(p["id"], {})
            state[p["id"]]["available"]    = available
            state[p["id"]]["last_checked"] = datetime.utcnow().isoformat()

            if available and not was_available:
                image_url = extract_og_image(html)
                send_notification(p, image_url)
                state[p["id"]]["notified_at"] = datetime.utcnow().isoformat()
                changed = True

            if i < len(tracked) - 1:
                time.sleep(REQUEST_DELAY)

        _browser.close()

    print("=" * 48)
    save_state(state)
    print(f"Done. {'Notifications sent!' if changed else 'No changes.'}")


if __name__ == "__main__":
    main()
