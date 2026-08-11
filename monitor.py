#!/usr/bin/env python3
"""
Hermès Japan Stock Monitor → Telegram Bot
Checks Hermès JP product pages and sends Telegram notifications on stock changes.

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
REQUEST_DELAY = 3   # seconds between product checks (be polite)
TIMEOUT       = 20  # HTTP timeout in seconds

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Referer": "https://www.hermes.com/jp/ja/",
}

# ── Availability detection ──────────────────────────────────────────────────────

def fetch_html(url: str) -> str | None:
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code == 200:
            return r.text
        print(f"  HTTP {r.status_code}")
        return None
    except requests.RequestException as e:
        print(f"  fetch error: {e}")
        return None


def extract_og_image(html: str) -> str | None:
    """Extract og:image meta tag value for use as Telegram photo."""
    patterns = [
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
    ]
    for pat in patterns:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def is_in_stock(html: str) -> bool:
    """Return True if the product page indicates in-stock status."""
    # Definitive out-of-stock signals — check first
    out_signals = [
        "OutOfStock",
        '"availability":"http://schema.org/OutOfStock"',
        '"availability": "http://schema.org/OutOfStock"',
        "product-not-available",
        "sold-out",
        "data-sold-out",
        "在庫なし",
    ]
    html_lower = html.lower()
    for s in out_signals:
        if s.lower() in html_lower:
            return False

    # In-stock signals in JSON-LD structured data or page markup
    in_signals = [
        '"availability":"http://schema.org/InStock"',
        '"availability": "http://schema.org/InStock"',
        '"availability":"InStock"',
        '"availability": "InStock"',
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

    # Try sending with photo first
    if image_url:
        r = requests.post(f"{TG_BASE}/sendPhoto", json={
            "chat_id":      CHAT_ID,
            "photo":        image_url,
            "caption":      caption,
            "parse_mode":   "HTML",
            "reply_markup": markup,
        }, timeout=TIMEOUT)
        if r.ok:
            print(f"  → Telegram photo sent ✓")
            return True
        print(f"  → sendPhoto failed ({r.status_code}), falling back to text")

    # Fallback: text-only message with link preview
    r = requests.post(f"{TG_BASE}/sendMessage", json={
        "chat_id":                  CHAT_ID,
        "text":                     caption,
        "parse_mode":               "HTML",
        "reply_markup":             markup,
        "disable_web_page_preview": False,
    }, timeout=TIMEOUT)

    if r.ok:
        print(f"  → Telegram message sent ✓")
        return True

    print(f"  → Telegram FAILED: {r.text}")
    return False


def send_test_message() -> None:
    """Send a test message to verify bot + chat_id are working."""
    r = requests.post(f"{TG_BASE}/sendMessage", json={
        "chat_id":    CHAT_ID,
        "text":       "✅ <b>HWatch Bot 接続確認</b>\n\nエルメス在庫監視ボットが正常に動作しています。",
        "parse_mode": "HTML",
    }, timeout=TIMEOUT)
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
    if not TOKEN or not CHAT_ID:
        print("ERROR: TELEGRAM_TOKEN and TELEGRAM_CHAT_ID must be set.")
        sys.exit(1)

    # --test flag: verify bot connection and exit
    if "--test" in sys.argv:
        send_test_message()
        return

    with open(PRODUCT_FILE, encoding="utf-8") as f:
        products = json.load(f)

    tracked = [p for p in products if p.get("tracked", True)]
    print(f"Monitoring {len(tracked)} products at {_jst_now()}")
    print("=" * 48)

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
            # New stock detected — notify!
            image_url = extract_og_image(html)
            send_notification(p, image_url)
            state[p["id"]]["notified_at"] = datetime.utcnow().isoformat()
            changed = True

        if i < len(tracked) - 1:
            time.sleep(REQUEST_DELAY)

    print("=" * 48)
    save_state(state)
    print(f"Done. {'Notifications sent!' if changed else 'No changes.'}")


if __name__ == "__main__":
    main()
