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
and report anything new.

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
# Label sources.  All the jazz series below are jazz-only by definition, so
# every new title qualifies.
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
        "id": "verve_vault",
        "label": "Verve \u2014 Vault Series",
        "base": "https://store.ververecords.com",
        "collection": "verve-vault",
        "keyword": "vault",
    },
    {
        # Analogue Productions is genre-mixed (jazz/rock/soul), so unlike the
        # five above it is NOT jazz-only by definition. We source it from The
        # 'In' Groove (a clean Shopify store that carries the full AP catalog and
        # doesn't IP-block bots the way AP's own store does), and keep it jazz-
        # only with zero AI by intersecting the AP label feed with In Groove's
        # Jazz genre collection. `require_kw` keeps only true AP titles (drops
        # In Groove exclusives / other labels that share the analog collection);
        # `exclude_kw` drops Verve "Acoustic Sounds Series" pressings already
        # covered by verve_acoustic.
        "id": "analogue_productions",
        "label": "Analogue Productions \u2014 Jazz",
        "base": "https://www.theingroove.com",
        "collection": "analog-production",
        "keyword": "analog",                  # fallback gate if collection 404s
        "genre_collection": "jazz-lps",       # intersect -> jazz only
        "require_kw": "analog",               # true AP titles only
        "exclude_kw": "acoustic sounds series",
    },
]

# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------
def _get_json(url):
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _fetch_collection(base, collection, keyword=None):
    """All products in one Shopify collection (paginated, 250/page). On 404 or
    network error, optionally fall back to the store-wide feed filtered by
    `keyword` in title/type/tags. Returns [] on total failure (never raises)."""
    products = []
    try:
        for page in range(1, 6):  # up to 1250 items, plenty
            url = f"{base}/collections/{collection}/products.json?limit=250&page={page}"
            batch = _get_json(url).get("products", [])
            if not batch:
                break
            products.extend(batch)
        if products:
            return products
    except Exception as e:  # 404 / network / parse -> try fallback
        msg = "; trying store-wide feed" if keyword else ""
        print(f"  [{collection}] collection feed failed ({e}){msg}")

    if not keyword:
        return products  # no fallback requested (e.g. a genre cross-ref feed)

    kw = keyword.lower()
    store_products = []
    for page in range(1, 11):
        url = f"{base}/products.json?limit=250&page={page}"
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


def fetch_products(src):
    """Return the raw products for one source.

    Most sources are a single jazz-only collection feed. A source may also be a
    genre-mixed label feed (e.g. Analogue Productions, which spans jazz/rock/
    soul). For those, we keep it jazz-only *without* any AI by intersecting the
    label feed with the store's own Jazz genre collection -- a release is kept
    only if the store itself files it under both. Optional title keyword gates
    then enforce that the item is really from this label (`require_kw`) and isn't
    a release already covered by another source (`exclude_kw`)."""
    products = _fetch_collection(src["base"], src["collection"], src.get("keyword"))

    # Genre cross-reference: keep only ids the store also lists under `genre_collection`.
    gc = src.get("genre_collection")
    if gc:
        genre = _fetch_collection(src["base"], gc)  # no keyword fallback for the cross-ref
        genre_ids = {p.get("id") for p in genre}
        before = len(products)
        products = [p for p in products if p.get("id") in genre_ids]
        print(f"  [{src['id']}] genre x-ref ({gc}): {before} -> {len(products)} jazz")

    # Title gates (case-insensitive substring on the product title).
    req = (src.get("require_kw") or "").lower()
    if req:
        products = [p for p in products if req in (p.get("title", "")).lower()]
    exc = (src.get("exclude_kw") or "").lower()
    if exc:
        products = [p for p in products if exc not in (p.get("title", "")).lower()]

    return products


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
    test pressings, multi-album bundles, and other non-LP merch so the app
    lists single records only."""
    t = (title or "").lower()
    bad = ("digital album", "(digital", "digital)", "test pressing",
           "t-shirt", "tshirt", "shirt", "hoodie", "poster", "slipmat",
           "bundle")
    if any(b in t for b in bad):
        return False
    if t.rstrip().endswith(" cd") or "(cd" in t:
        return False
    # Two-album packs are sold as one product with the titles joined by " + "
    # (e.g. "Coltrane (...) + Cookin' With (...)"). Those aren't a single LP;
    # the individual albums are listed separately, so drop the combined product.
    if " + " in (title or ""):
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
# State
# ---------------------------------------------------------------------------
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def prune_state(state, valid_ids):
    """Drop per-source state for sources that no longer exist, plus the now-unused
    classifier cache. Keeps reserved keys (those starting with '_') except the
    classifier cache. Prevents stale baselines from old/removed sources (e.g. a
    long line of abandoned Rhino/AP experiments) lingering in seen.json and
    causing odd diffs if a similarly-named source is ever re-added."""
    removed = []
    for key in list(state.keys()):
        if key.startswith("_"):
            if key == "_ai_genre":          # classifier is gone; cache is dead weight
                del state[key]
                removed.append(key)
            continue                        # keep _first_seen and any other reserved
        if key not in valid_ids:
            del state[key]
            removed.append(key)
    if removed:
        print(f"Pruned stale state keys: {', '.join(removed)}")
    return bool(removed)


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


def _norm_title(t):
    """Normalise a title for cross-source dedup: lowercase, strip the trailing
    label/series suffix in parens, collapse punctuation/whitespace."""
    t = (t or "").lower()
    t = re.sub(r"\s*\([^)]*\)\s*$", "", t)        # drop trailing "(...series...)"
    t = t.replace("\u2014", " ").replace("-", " ")
    t = re.sub(r"[^a-z0-9 ]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def write_data_json(catalog):
    """Write the full catalog (all current items + blurbs) for the app to read.
    Dedups on id, then on exact normalised title within a label, so a record
    reaching the catalog via more than one path (collection feed + keyword
    fallback) appears once. First occurrence wins.

    NB: an EXACT title match is used, not containment. Containment was too
    greedy -- it collapsed distinct albums whose names are substrings of a
    larger title (e.g. a "A + B" two-LP pack swallowing the solo "A" and "B"
    releases, or a boxset swallowing its constituent albums)."""
    seen_ids = set()
    kept_titles = set()         # (label_id, normalised_title)
    deduped = []
    for it in catalog:
        iid = it.get("id")
        if iid in seen_ids:
            continue
        key = (it.get("label_id"), _norm_title(it.get("title")))
        if key[1] and key in kept_titles:
            continue
        seen_ids.add(iid)
        kept_titles.add(key)
        deduped.append(it)
    dropped = len(catalog) - len(deduped)
    if dropped:
        print(f"  (deduped {dropped} duplicate item(s) on id/title)")
    payload = {
        "updated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "count": len(deduped),
        "items": deduped,
    }
    DATA_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(f"Wrote {DATA_FILE.name} ({len(deduped)} items).")


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


# first_seen: the date THIS tracker first encountered each release id. Stored
# under the reserved "_first_seen" key in seen.json so it persists across runs
# and is never overwritten. This is what the app sorts on -- unlike Shopify's
# created_at/published_at (store-listing dates that jump around on re-publish),
# first_seen reliably puts genuinely newly-detected releases on top. On the very
# first run after this is added, everything gets stamped "now" (one-time
# backfill); from then on only truly new ids get a fresh stamp.
def stamp_first_seen(state, catalog):
    fs = state.setdefault("_first_seen", {})
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    for it in catalog:
        rid = it["id"]
        if rid not in fs:
            fs[rid] = now
        it["first_seen"] = fs[rid]


def main():
    state = load_state()
    blurbs = load_blurbs()
    all_new = []        # list of (label, [items])
    catalog = []        # every current item across all sources, for the app
    changed = False

    # Drop state from sources that no longer exist (keeps seen.json honest).
    if prune_state(state, {s["id"] for s in SOURCES}):
        changed = True

    # Shopify-based label feeds.
    for src in SOURCES:
        print(f"Checking {src['label']} ...")
        try:
            raw = fetch_products(src)
        except Exception as e:
            print(f"  ERROR fetching {src['id']}: {e}")
            continue
        items = {}
        for p in raw:
            it = simplify(src, p)
            if not is_lp(it["title"]):
                continue
            items[it["id"]] = it
        print(f"  found {len(items)} LPs")
        for it in items.values():
            it["label_name"] = src["label"]
            catalog.append(it)
        if diff_source(state, src["id"], src["label"], items, all_new):
            changed = True

    # Attach hand-curated blurbs. There are NO automatic AI calls: blurbs are
    # written on demand (a consensus summary for a specific title) and stored in
    # manual_blurbs.json as { release_id: text }. Merging here means curated
    # blurbs survive every catalog refresh instead of being overwritten.
    for it in catalog:
        it["blurb"] = blurbs.get(it["id"], "")
    print(f"Attached {sum(1 for it in catalog if it['blurb'])} curated blurb(s).")

    # Stamp each item with first_seen (when this tracker first saw it). New ids
    # get "now"; existing ids keep their original stamp. The app sorts on this.
    before = len(state.get("_first_seen", {}))
    stamp_first_seen(state, catalog)
    if len(state["_first_seen"]) != before:
        changed = True   # new stamps -> persist state

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
