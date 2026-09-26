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
import html as _htmllib
import json
import os
import re
import smtplib
import sys
import time
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import quote_plus

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
        "relink_official": True,              # click -> Acoustic Sounds, not the retailer
    },
]

# ---------------------------------------------------------------------------
# Forward-release calendar (upcomingvinyl.com)
# ---------------------------------------------------------------------------
# The Shopify feeds above only show what a store has already listed. They can't
# tell us a record is *coming* until it appears for sale. upcomingvinyl.com is a
# release calendar that lists forward-dated pressings per label, so we scrape the
# handful of labels we already track to fold a real release_date into the app
# (and to surface titles before they hit the stores).
#
# Design notes / hidden issues to be aware of:
#   * This is HTML scraping of a third-party site (no API), so it is inherently
#     more fragile than the Shopify JSON feeds. It is wrapped so any failure is
#     non-fatal: the monitor logs and carries on with the Shopify catalog.
#   * We target *stable* signals only -- semantic URL paths (/release-date/,
#     /style/, /discs/, /record/) and og: meta tags -- not visual layout.
#   * upcomingvinyl's label taxonomy is coarser than ours: Blue Note / Verve /
#     Craft each host several series (incl. non-audiophile ones like Blue Note
#     "Essential"). We mirror each Shopify source's SERIES by requiring a slug
#     marker (`slug_kw`); Analogue Prod. is genre-mixed so it uses a genre gate
#     (`require_style`) instead, exactly like the In Groove jazz cross-ref.
#   * Each record page is fetched at most once, then cached in seen.json under
#     "_upcoming", so steady-state traffic is ~6 label pages + any new records.
UV_BASE = "https://upcomingvinyl.com"

UPCOMING = [
    {"label_id": "bluenote_tone_poet", "uv": "blue-note",       "slug_kw": "tone-poet"},
    {"label_id": "bluenote_classic",   "uv": "blue-note",       "slug_kw": "classic-vinyl"},
    {"label_id": "craft_ojc",          "uv": "craft-recordings","slug_kw": "original-jazz-classics"},
    {"label_id": "verve_acoustic",     "uv": "verve",           "slug_kw": "acoustic-sounds"},
    {"label_id": "verve_vault",        "uv": "verve",           "slug_kw": "vault"},
    {"label_id": "analogue_productions","uv": "analogue-prod",  "slug_kw": None, "require_style": "jazz"},
]

# ---------------------------------------------------------------------------
# Official-store links
# ---------------------------------------------------------------------------
# Every LP should click through to the official label store. Blue Note, Craft
# and Verve are sourced from their own Shopify stores, so their product URLs are
# already official and are left untouched. The two exceptions are items sourced
# via a retailer (Analogue Productions, read from The 'In' Groove) or the
# upcomingvinyl calendar -- for those we build an official-store link instead and
# keep the original page under `source_url` (surfaced as a small "details" link).
#
# Shopify stores expose /search?q=. Analogue Productions' own store, Acoustic
# Sounds, is not Shopify; its product pages are /d/<id>/<slug> (the id isn't
# derivable from a third-party feed), so we link to its keyword search
# (get=results&SearchText=) -- the parameter its own search form uses.
OFFICIAL_STORE = {
    "bluenote_tone_poet":   "https://store.bluenote.com",
    "bluenote_classic":     "https://store.bluenote.com",
    "craft_ojc":            "https://craftrecordings.com",
    "verve_acoustic":       "https://store.ververecords.com",
    "verve_vault":          "https://store.ververecords.com",
    "analogue_productions": "https://store.acousticsounds.com",
}


def _search_query(title):
    """A clean 'artist album' query from a store/aggregator title: drop any
    parenthetical (colour/variant/series) and the trailing ' - <label> LP'
    tail, keeping the artist and album."""
    t = re.sub(r"\([^)]*\)", " ", title or "")
    parts = re.split(r"\s+[–—-]\s+", t)      # split on ' - ' / en/em dash
    core = " ".join(parts[:2]) if len(parts) >= 2 else (parts[0] if parts else "")
    core = re.sub(r"\b(180\s?-?\s?gram|200\s?-?\s?gram|180g|200g|45\s?rpm|33\s?rpm"
                  r"|[234]\s?x?lp|lp|mono|stereo)\b", " ", core, flags=re.I)
    return re.sub(r"\s+", " ", core).strip()


def official_url(label_id, title):
    """Best official-store link for a title, or None if the label is unknown."""
    base = OFFICIAL_STORE.get(label_id)
    if not base:
        return None
    q = quote_plus(_search_query(title))
    if "acousticsounds.com" in base:
        return f"{base}/index.cfm?get=results&SearchText={q}"
    return f"{base}/search?q={q}"                       # Shopify storefront search

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
    title = p.get("title", "").strip()
    url = f"{src['base']}/products/{handle}" if handle else src["base"]
    item = {
        "id": str(p.get("id")),
        "label_id": src["id"],
        "title": title,
        "url": url,
        "price": price,
        "image": image,
        "published_at": p.get("published_at"),
        "created_at": created,
        "specs": _specs_from(f"{p.get('title','')} {' '.join(p.get('tags') or [])} {body}"),
        "preorder": ("pre-order" in blob or "preorder" in blob or not any_available),
    }
    # Sources read via a retailer (e.g. Analogue Productions via In Groove) point
    # the click at the official store instead, keeping the retailer as a fallback.
    if src.get("relink_official"):
        off = official_url(src["id"], title)
        if off:
            item["source_url"] = url
            item["url"] = off
    return item


# ---------------------------------------------------------------------------
# upcomingvinyl.com scraping (forward release dates)
# ---------------------------------------------------------------------------
def _uv_get(url):
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def _uv_meta(html, prop):
    """Content of an og:/twitter: meta tag, attribute order agnostic."""
    p = re.escape(prop)
    m = re.search(r'<meta[^>]+?(?:property|name)=["\']' + p +
                  r'["\'][^>]*?content=["\']([^"\']*)["\']', html, re.I)
    if not m:
        m = re.search(r'<meta[^>]+?content=["\']([^"\']*)["\'][^>]*?(?:property|name)=["\']' +
                      p + r'["\']', html, re.I)
    return _htmllib.unescape(m.group(1)).strip() if m else None


_UV_SLUG_RE  = re.compile(r'/record/([a-z0-9][a-z0-9\-]*)')
_UV_DATE_RE  = re.compile(r'/release-date/(\d{4}-\d{2}-\d{2})')
_UV_STYLE_RE = re.compile(r'/style/([a-z0-9\-]+)')
_UV_DISCS_RE = re.compile(r'/discs/(\d+)')
_UV_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], 1)}


def _uv_record_slugs(uv_label):
    """Ordered, de-duplicated record slugs listed on a label page. Label pages
    are clean lists (no recommendation sidebars), so every /record/ link is a
    genuine upcoming release for that label."""
    html = _uv_get(f"{UV_BASE}/label/{uv_label}")
    slugs, seen = [], set()
    for m in _UV_SLUG_RE.finditer(html):
        s = m.group(1)
        if s not in seen:
            seen.add(s)
            slugs.append(s)
    return slugs


def _uv_date_from_desc(html):
    """Fallback: 'Released October 2, 2026' out of the og/meta description."""
    desc = _uv_meta(html, "og:description") or _uv_meta(html, "description") or ""
    m = re.search(r'([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})', desc)
    if not m:
        return None
    mon = _UV_MONTHS.get(m.group(1).lower())
    if not mon:
        return None
    return f"{int(m.group(3)):04d}-{mon:02d}-{int(m.group(2)):02d}"


def _uv_parse_record(slug, html):
    """Structured fields for one record page. Sidebars ('Last added',
    'Featured') carry unrelated /record/ links and plain-text dates, so we read
    genre/date/format only from the head of the page, before those sections."""
    cut = len(html)
    for marker in ("LAST ADDED", "Featured Upcoming", "Recommended equipment"):
        i = html.find(marker)
        if i != -1:
            cut = min(cut, i)
    head = html[:cut]

    dm = _UV_DATE_RE.search(head)
    release_date = dm.group(1) if dm else _uv_date_from_desc(html)
    styles = sorted(set(_UV_STYLE_RE.findall(head)))
    disc_m = _UV_DISCS_RE.search(head)
    discs = int(disc_m.group(1)) if disc_m else 1

    title = _uv_meta(html, "og:title")
    if not title:
        raw = _uv_meta(html, "title") or slug
        title = re.split(r'\s*[|\[]', raw)[0].strip()
    image = _uv_meta(html, "og:image")
    if image and "og-image" in image:      # generic fallback image, not a cover
        image = None
    return {
        "slug": slug,
        "title": title,
        "release_date": release_date,
        "styles": styles,
        "discs": discs,
        "image": image,
        "url": f"{UV_BASE}/record/{slug}",
    }


def _uv_to_item(label_id, rec):
    specs = _specs_from(rec["title"])
    if rec.get("discs", 1) and rec["discs"] > 1 and f"{rec['discs']}xLP" not in specs:
        specs = [f"{rec['discs']}xLP"] + specs
    return {
        "id": "uv_" + rec["slug"],
        "label_id": label_id,
        "label_name": _label_name_for(label_id),
        "title": rec["title"],
        "url": official_url(label_id, rec["title"]) or rec["url"],
        "source_url": rec["url"],           # upcomingvinyl page (tracklist / pre-order)
        "price": None,
        "image": rec.get("image"),
        "published_at": None,
        "created_at": rec.get("release_date") or "",
        "release_date": rec.get("release_date"),
        "specs": specs,
        "preorder": True,
        "upcoming": True,
    }


def _label_name_for(label_id):
    for s in SOURCES:
        if s["id"] == label_id:
            return s["label"]
    return label_id


def fetch_upcoming(state):
    """Scrape upcomingvinyl.com for forward-dated releases on the labels/series
    we track. Returns (items, new_slugs, is_baseline). Record pages are cached in
    state['_upcoming'] so each is fetched at most once. Fully defensive: a failed
    label or record page is logged and skipped, never raised."""
    is_baseline = "_upcoming" not in state
    cache = state.setdefault("_upcoming", {})
    today = datetime.date.today().isoformat()
    stale = (datetime.date.today() - datetime.timedelta(days=45)).isoformat()

    # Fetch each label page once, even when it feeds several series.
    by_uv = {}
    for cfg in UPCOMING:
        by_uv.setdefault(cfg["uv"], []).append(cfg)

    items, new_slugs = [], []
    for uv_label, cfgs in by_uv.items():
        try:
            slugs = _uv_record_slugs(uv_label)
        except Exception as e:
            print(f"  [upcoming/{uv_label}] label page failed: {e}")
            continue
        print(f"  [upcoming/{uv_label}] {len(slugs)} listed")
        for slug in slugs:
            matched = [c for c in cfgs
                       if c.get("slug_kw") is None or c["slug_kw"] in slug]
            if not matched:
                continue
            rec = cache.get(slug)
            if rec is None:
                try:
                    rec = _uv_parse_record(slug, _uv_get(f"{UV_BASE}/record/{slug}"))
                except Exception as e:
                    print(f"  [upcoming] record {slug} failed: {e}")
                    continue
                cache[slug] = rec
                new_slugs.append(slug)
                time.sleep(0.4)             # be polite to the site
            for c in matched:
                if c.get("require_style") and c["require_style"] not in rec.get("styles", []):
                    continue
                items.append(_uv_to_item(c["label_id"], rec))
                break

    # Drop anything already released (it belongs to the Shopify feeds now) and
    # prune long-past entries from the cache so seen.json doesn't grow forever.
    items = [it for it in items if not it["release_date"] or it["release_date"] >= today]
    for s in list(cache.keys()):
        rd = cache[s].get("release_date")
        if rd and rd < stale:
            del cache[s]
    return items, new_slugs, is_baseline


def _match_key(title):
    """Aggressive core key (artist+album) for matching an upcomingvinyl title to
    an already-listed Shopify title, which carries extra label/series/format
    words. Looser than _norm_title on purpose."""
    t = (title or "").lower()
    t = re.sub(r"\([^)]*\)", " ", t)                 # drop ALL parentheticals
    for tok in ("analogue productions", "analog productions", "acoustic sounds series",
                "acoustic sounds", "original jazz classics series", "original jazz classics",
                "tone poet", "classic vinyl", "vault series", "vault", "blue note",
                "verve", "craft recordings", "series", "reissue", "180g", "200g",
                "45rpm", "45 rpm", "33rpm", "2xlp", "3xlp", "4xlp", "lp"):
        t = t.replace(tok, " ")
    t = t.replace("—", " ").replace("-", " ")
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def merge_upcoming(catalog, upcoming):
    """Fold upcoming items into the catalog. If an upcoming release already has a
    Shopify listing (same label, matching core title), attach its release_date to
    that listing and drop the duplicate. Otherwise append it as a standalone
    'upcoming' entry. Returns the list of appended (not-yet-listed) items."""
    index = {}
    for it in catalog:
        index.setdefault((it["label_id"], _match_key(it["title"])), it)
    appended = []
    for u in upcoming:
        key = (u["label_id"], _match_key(u["title"]))
        m = index.get(key)
        if m:
            if u.get("release_date") and not m.get("release_date"):
                m["release_date"] = u["release_date"]
        else:
            catalog.append(u)
            index[key] = u
            appended.append(u)
    return appended


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
    rd = it.get("release_date")
    date = f"  (out {rd})" if rd else ""
    return f"\u2022 {it['title']}{price}{date}{tag}\n  {it['url']}"


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

    # Forward release dates from upcomingvinyl.com. Fully non-fatal: any failure
    # here leaves the Shopify catalog above untouched.
    print("Checking upcomingvinyl.com ...")
    try:
        upcoming, new_slugs, uv_baseline = fetch_upcoming(state)
        appended = merge_upcoming(catalog, upcoming)
        print(f"  {len(upcoming)} upcoming item(s); {len(appended)} not yet in stores")
        if new_slugs or uv_baseline:
            changed = True
        if uv_baseline:
            print("  baseline recorded (no upcoming alert on first run)")
        elif new_slugs:
            ns = set(new_slugs)
            fresh = [u for u in upcoming if u["id"][3:] in ns]
            if fresh:
                all_new.append(("⏳ Upcoming releases", fresh))
                print(f"  {len(fresh)} NEW upcoming")
    except Exception as e:
        print(f"  upcoming feed error: {e}")

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
