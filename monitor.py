#!/usr/bin/env python3
"""
Jazz Vinyl Reissue Monitor
==========================
Checks the official label stores for newly added jazz reissue LPs and sends a
notification (Discord / Telegram / e-mail) only when something NEW appears.

How it works
------------
Almost every audiophile reissue label runs its webshop on Shopify, and Shopify
exposes a clean JSON feed at  {store}/collections/{handle}/products.json .
That is far more reliable than scraping HTML. We read that feed per label,
compare the product IDs against the ones we saw last time (stored in seen.json),
and report anything new.

First run is treated as a BASELINE: it records what currently exists without
spamming you. From the second run on, only genuine additions are reported.

Configure notifications by setting the relevant environment variables (secrets).
You only need ONE channel; set whichever you like and leave the rest empty.
"""

import json
import os
import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path

import requests

STATE_FILE = Path(__file__).with_name("seen.json")
UA = {"User-Agent": "Mozilla/5.0 (compatible; JazzVinylMonitor/1.0)"}
TIMEOUT = 30

# ---------------------------------------------------------------------------
# Label sources.  All four jazz series below are jazz-only by definition, so
# every new title qualifies.  Rhino High Fidelity is mostly rock with the odd
# jazz title (e.g. Coltrane) and is very low volume (~2 per quarter), so we
# report everything new there and let you judge.
#
# `collection` = Shopify collection handle. If it ever 404s, the script falls
# back to the store-wide feed and filters by `keyword`, so a renamed handle
# won't silently break the monitor.
# ---------------------------------------------------------------------------
SOURCES = [
    {
        "id": "bluenote_tone_poet",
        "label": "Blue Note \u2014 Tone Poet Series",
        "base": "https://store.bluenote.com",
        "collection": "tone-poet-series",
        "keyword": "tone poet",
    },
    {
        "id": "bluenote_classic",
        "label": "Blue Note \u2014 Classic Vinyl Series",
        "base": "https://store.bluenote.com",
        "collection": "classic-vinyl-series",
        "keyword": "classic vinyl",
    },
    {
        "id": "craft_ojc",
        "label": "Craft Recordings \u2014 OJC Series",
        "base": "https://craftrecordings.com",
        "collection": "original-jazz-classics",
        "keyword": "original jazz classics",
    },
    {
        "id": "verve_acoustic",
        "label": "Verve \u2014 Acoustic Sounds Series",
        "base": "https://store.ververecords.com",
        "collection": "acoustic-sounds",
        "keyword": "acoustic sounds",
    },
    {
        # NOTE: Rhino store handle is unverified. The store-wide fallback filter
        # ("high fidelity") will catch it even if the handle below is wrong.
        # Check the first-run log; if Rhino shows 0 and an error, adjust `base`
        # or `collection` here only.
        "id": "rhino_hifi",
        "label": "Rhino \u2014 High Fidelity Series",
        "base": "https://www.rhino.com",
        "collection": "rhino-high-fidelity",
        "keyword": "high fidelity",
    },
]


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def _get_json(url):
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def fetch_products(src):
    """Return a list of product dicts for one label, with handle fallback."""
    # Primary: the specific collection feed (paginated, 250/page).
    products = []
    try:
        for page in range(1, 6):  # up to 1250 items, plenty
            url = f"{src['base']}/collections/{src['collection']}/products.json?limit=250&page={page}"
            batch = _get_json(url).get("products", [])
            if not batch:
                break
            products.extend(batch)
        if products:
            return products
    except Exception as e:  # 404 / network / parse -> try fallback
        print(f"  [{src['id']}] collection feed failed ({e}); trying store-wide feed")

    # Fallback: whole store, filtered by keyword in title/type/tags.
    kw = src["keyword"].lower()
    store_products = []
    for page in range(1, 11):
        url = f"{src['base']}/products.json?limit=250&page={page}"
        batch = _get_json(url).get("products", [])
        if not batch:
            break
        store_products.extend(batch)
    filtered = []
    for p in store_products:
        hay = " ".join(
            [p.get("title", ""), p.get("product_type", "")] + (p.get("tags") or [])
        ).lower()
        if kw in hay:
            filtered.append(p)
    return filtered


def simplify(src, p):
    """Pull the fields we care about out of a raw Shopify product."""
    price = None
    variants = p.get("variants") or []
    if variants:
        price = variants[0].get("price")
    image = None
    images = p.get("images") or []
    if images:
        image = images[0].get("src")
    handle = p.get("handle", "")
    return {
        "id": str(p.get("id")),
        "title": p.get("title", "").strip(),
        "url": f"{src['base']}/products/{handle}" if handle else src["base"],
        "price": price,
        "image": image,
        "published_at": p.get("published_at"),
    }


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Notifications  (set only the channel you want via env vars)
# ---------------------------------------------------------------------------
def fmt_item(it):
    price = f" \u2014 ${it['price']}" if it.get("price") else ""
    return f"\u2022 {it['title']}{price}\n  {it['url']}"


def send_discord(text):
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        return
    # Discord caps content at 2000 chars; chunk if needed.
    for chunk in _chunks(text, 1900):
        requests.post(url, json={"content": chunk}, timeout=TIMEOUT)


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return
    for chunk in _chunks(text, 3900):
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": chunk, "disable_web_page_preview": True},
            timeout=TIMEOUT,
        )


def send_email(subject, text):
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    pwd = os.environ.get("SMTP_PASS")
    to = os.environ.get("EMAIL_TO")
    if not (host and user and pwd and to):
        return
    port = int(os.environ.get("SMTP_PORT", "465"))
    msg = MIMEText(text, _charset="utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    with smtplib.SMTP_SSL(host, port, timeout=TIMEOUT) as s:
        s.login(user, pwd)
        s.sendmail(user, [to], msg.as_string())


def _chunks(s, n):
    lines, buf = s.split("\n"), ""
    for line in lines:
        if len(buf) + len(line) + 1 > n:
            yield buf
            buf = ""
        buf += line + "\n"
    if buf.strip():
        yield buf


def notify(subject, body):
    print("\n" + subject + "\n" + body)  # always log
    send_discord(f"**{subject}**\n{body}")
    send_telegram(f"{subject}\n{body}")
    send_email(subject, body)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    state = load_state()
    all_new = []        # list of (label, [items])
    changed = False

    for src in SOURCES:
        print(f"Checking {src['label']} ...")
        try:
            raw = fetch_products(src)
        except Exception as e:
            print(f"  ERROR fetching {src['id']}: {e}")
            continue

        items = {it["id"]: it for it in (simplify(src, p) for p in raw)}
        print(f"  found {len(items)} products")

        prev = state.get(src["id"])
        if prev is None:
            # First time we see this label -> baseline, do not notify.
            state[src["id"]] = items
            changed = True
            print(f"  baseline recorded ({len(items)} items, no alert)")
            continue

        new_ids = [i for i in items if i not in prev]
        if new_ids:
            new_items = [items[i] for i in new_ids]
            all_new.append((src["label"], new_items))
            print(f"  {len(new_items)} NEW")

        # Merge (keep old entries too so removed/sold-out items don't re-alert).
        merged = dict(prev)
        merged.update(items)
        if merged != prev:
            state[src["id"]] = merged
            changed = True

    if all_new:
        sections = []
        total = 0
        for label, items in all_new:
            total += len(items)
            body = "\n".join(fmt_item(it) for it in items)
            sections.append(f"\u25b6 {label}\n{body}")
        subject = f"\U0001f3b7 {total} new jazz vinyl reissue(s)"
        notify(subject, "\n\n".join(sections))
    else:
        print("\nNo new releases.")

    if changed:
        save_state(state)
        print("State updated.")


if __name__ == "__main__":
    sys.exit(main())
