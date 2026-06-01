#!/usr/bin/env python3
"""
Jazz Vinyl Reissue Monitor
==========================
Checks the official label stores for newly added jazz reissue LPs and sends a
notification (Discord / Telegram / e-mail) only when something NEW appears.

How it works
------------
Most audiophile reissue labels run their webshop on Shopify, which exposes a
clean JSON feed at  {store}/collections/{handle}/products.json . We read that
feed per label, compare product IDs against what we saw last time (seen.json),
and report anything new. Rhino High Fidelity is the exception: Rhino's own store
blocks bots, so its JAZZ titles are scraped from the Acoustic Sounds retailer
page instead (HTML, so a bit more fragile).

First run is treated as a BASELINE: it records what currently exists without
spamming you. From the second run on, only genuine additions are reported.

Configure notifications by setting the relevant environment variables (secrets).
You only need ONE channel; set whichever you like and leave the rest empty.
"""

import datetime
import json
import os
import re
import smtplib
import sys
from email.mime.text import MIMEText
from pathlib import Path

import requests

STATE_FILE = Path(__file__).with_name("seen.json")
DATA_FILE = Path(__file__).with_name("data.json")       # full catalog for the app
BLURB_FILE = Path(__file__).with_name("manual_blurbs.json")  # hand-curated blurbs { id: text }
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


def _specs_from(text):
    t = (text or "").lower()
    s = []
    if re.search(r"\b(aaa|all[\s-]?analog(ue)?)\b", t): s.append("AAA")
    if re.search(r"200\s?-?\s?(g|gram|gr)\b", t): s.append("200g")
    elif re.search(r"180\s?-?\s?(g|gram|gr)\b", t): s.append("180g")
    if re.search(r"\b45\s?-?\s?rpm\b", t): s.append("45 RPM")
    if re.search(r"\bmono\b", t): s.append("Mono")
    if re.search(r"\bgatefold\b", t): s.append("Gatefold")
    return s


def is_lp(title):
    """True only for vinyl LPs -- excludes digital albums, CDs, t-shirts,
    test pressings, and other non-LP merch so the app lists records only."""
    t = (title or "").lower()
    bad = ("digital album", "(digital", "digital)", "test pressing",
           "t-shirt", "tshirt", "shirt", "hoodie", "poster", "slipmat")
    if any(b in t for b in bad):
        return False
    if t.rstrip().endswith(" cd") or "(cd" in t:
        return False
    return True


def simplify(src, p):
    """Pull the fields we care about out of a raw Shopify product."""
    variants = p.get("variants") or []
    price = variants[0].get("price") if variants else None
    images = p.get("images") or []
    image = images[0].get("src") if images else None
    handle = p.get("handle", "")
    body = p.get("body_html", "") or ""
    blob = f"{p.get('title','')} {' '.join(p.get('tags') or [])} {body}".lower()
    any_available = any(v.get("available") for v in variants)
    created = p.get("created_at") or p.get("published_at") or ""
    return {
        "id": str(p.get("id")),
        "label_id": src["id"],
        "title": p.get("title", "").strip(),
        "url": f"{src['base']}/products/{handle}" if handle else src["base"],
        "price": price,
        "image": image,
        "published_at": p.get("published_at"),
        "created_at": created,
        "specs": _specs_from(f"{p.get('title','')} {' '.join(p.get('tags') or [])} {body}"),
        "preorder": ("pre-order" in blob or "preorder" in blob or not any_available),
    }


# ---------------------------------------------------------------------------
# Rhino High Fidelity -- JAZZ ONLY, hand-curated in manual_rhino.json.
# Why manual: Rhino's own store blocks bots, Discogs blocks bots and only
# catalogues the series buried in the full Rhino label (no clean "RHF jazz"
# slice), and the titles that matter are deliberately scattered, low-volume,
# indie-retail exclusives with no single feed. Chasing that with a scraper is
# fragile and still misses the exclusives. So RHF jazz is curated by hand: add
# a title to manual_rhino.json and it shows up on the next run. The file is a
# JSON list of objects, e.g.:
#   [
#     {
#       "id": "rhino_my_favorite_things_2026",   # any stable unique string
#       "title": "John Coltrane - My Favorite Things (Rhino High Fidelity 2026)",
#       "url": "https://elusivedisc.com/...",
#       "price": "39.99",            # string or null
#       "image": "https://...",      # cover URL or null (app handles missing)
#       "specs": ["AAA", "180g", "Mono", "Gatefold"],
#       "preorder": false,
#       "published_at": ""           # ISO date if known, else ""
#     }
#   ]
# ---------------------------------------------------------------------------
RHINO_LABEL = "Rhino \u2014 High Fidelity (jazz)"
RHINO_FILE = Path(__file__).with_name("manual_rhino.json")


def fetch_rhino_jazz():
    """Load hand-curated Rhino High Fidelity jazz titles from manual_rhino.json."""
    if not RHINO_FILE.exists():
        return []
    try:
        entries = json.loads(RHINO_FILE.read_text())
    except Exception as e:
        print(f"  could not read {RHINO_FILE.name}: {e}")
        return []
    items = []
    for e in entries:
        if not e.get("id") or not e.get("title"):
            continue  # skip malformed entries
        items.append({
            "id": str(e["id"]),
            "title": e["title"],
            "url": e.get("url", ""),
            "price": e.get("price"),
            "image": e.get("image"),
            "specs": e.get("specs", []),
            "preorder": bool(e.get("preorder", False)),
            "published_at": e.get("published_at", ""),
        })
    return items


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
    avail = it.get("availability")
    tag = f"  [{avail}]" if avail and avail.lower() != "in stock" else ""
    return f"\u2022 {it['title']}{price}{tag}\n  {it['url']}"


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
# Hand-curated blurbs
# ---------------------------------------------------------------------------
# Blurbs are written on demand (a consensus summary for a specific title) and
# kept in manual_blurbs.json as { release_id: text }. There are no automatic
# AI calls anywhere in this script. To add one, put the text under the
# release's id in manual_blurbs.json; it shows in the app on the next run.
def load_blurbs():
    if BLURB_FILE.exists():
        try:
            return json.loads(BLURB_FILE.read_text())
        except Exception:
            return {}
    return {}


def write_data_json(catalog):
    """Write the full catalog (all current items + blurbs) for the app to read."""
    payload = {
        "updated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "count": len(catalog),
        "items": catalog,
    }
    DATA_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"Wrote {DATA_FILE.name} ({len(catalog)} items).")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def diff_source(state, sid, label, items, all_new):
    """Update state for one source; record new items. Returns True if changed."""
    prev = state.get(sid)
    if prev is None:                      # first run -> baseline, no alert
        state[sid] = items
        print(f"  baseline recorded ({len(items)} items, no alert)")
        return True
    new_ids = [i for i in items if i not in prev]
    if new_ids:
        all_new.append((label, [items[i] for i in new_ids]))
        print(f"  {len(new_ids)} NEW")
    merged = dict(prev)                   # keep old ids so sold-out won't re-alert
    merged.update(items)
    if merged != prev:
        state[sid] = merged
        return True
    return False


def main():
    state = load_state()
    blurbs = load_blurbs()
    all_new = []        # list of (label, [items])
    catalog = []        # every current item across all sources, for the app
    changed = False

    # Shopify-based label feeds.
    for src in SOURCES:
        print(f"Checking {src['label']} ...")
        try:
            raw = fetch_products(src)
        except Exception as e:
            print(f"  ERROR fetching {src['id']}: {e}")
            continue
        items = {it["id"]: it for it in (simplify(src, p) for p in raw)
                 if is_lp(it["title"])}
        print(f"  found {len(items)} LPs")
        for it in items.values():
            it["label_name"] = src["label"]
            catalog.append(it)
        if diff_source(state, src["id"], src["label"], items, all_new):
            changed = True

    # Rhino High Fidelity (jazz only) -- hand-curated in manual_rhino.json.
    print(f"Checking {RHINO_LABEL} ...")
    try:
        ritems = {it["id"]: it for it in fetch_rhino_jazz()}
        print(f"  found {len(ritems)} curated titles")
        for it in ritems.values():
            it["label_name"] = RHINO_LABEL
            catalog.append(it)
        if diff_source(state, "rhino_hifi_jazz", RHINO_LABEL, ritems, all_new):
            changed = True
    except Exception as e:
        print(f"  ERROR loading Rhino titles: {e}")

    # Attach hand-curated blurbs. There are NO automatic AI calls: blurbs are
    # written on demand (a consensus summary for a specific title) and stored in
    # manual_blurbs.json as { release_id: text }. Merging here means curated
    # blurbs survive every catalog refresh instead of being overwritten.
    for it in catalog:
        it["blurb"] = blurbs.get(it["id"], "")
    print(f"Attached {sum(1 for it in catalog if it['blurb'])} curated blurb(s).")

    # Write the catalog file the app reads.
    write_data_json(catalog)

    # Email/notify only about genuinely new releases (unchanged behaviour).
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
