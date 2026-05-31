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
BLURB_FILE = Path(__file__).with_name("blurbs.json")    # cached AI consensus blurbs
UA = {"User-Agent": "Mozilla/5.0 (compatible; JazzVinylMonitor/1.0)"}
TIMEOUT = 30
ANTHROPIC_MODEL = "claude-3-5-haiku-latest"   # cheap; fine for short blurbs

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
# Rhino High Fidelity -- JAZZ ONLY, scraped from Acoustic Sounds.
# Rhino's own store blocks bots (HTTP 403). Acoustic Sounds (a retailer) carries
# the series but isn't Shopify, so there's no JSON feed -- we parse its HTML.
# Each product page is kept ONLY if its Label is Rhino High Fidelity AND its
# Genre is Jazz, so rock/pop titles in the series are filtered out.
# This source is inherently more fragile than the Shopify feeds; if Acoustic
# Sounds restyles its pages it may need the regexes below updated.
# ---------------------------------------------------------------------------
RHINO_AS = {
    "id": "rhino_hifi_jazz",
    "label": "Rhino \u2014 High Fidelity (jazz, via Acoustic Sounds)",
    "listing": "https://store.acousticsounds.com/l/10065/Rhino_High_Fidelity",
    "base": "https://store.acousticsounds.com",
    "label_path": "/l/10065/",   # appears on a page only if it's a Rhino Hi-Fi title
}
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}


def _get_html(url):
    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def _parse_release_date(html):
    """Find an upcoming ship/reissue date in the page text; '' if none."""
    text = re.sub(r"<[^>]+>", " ", html)
    m = re.search(
        r"(?:ship|reissu\w*|arriv\w*|expected|due)\b[^.]{0,60}?"
        r"\b(january|february|march|april|may|june|july|august|"
        r"september|october|november|december)\s+(\d{1,2}),?\s+(\d{4})",
        text, re.I)
    if m:
        try:
            return datetime.date(
                int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2))
            ).isoformat()
        except Exception:
            pass
    m = re.search(r"(?:ship|reissu\w*)\b[^.]{0,40}?(\d{1,2})/(\d{1,2})/(\d{2,4})",
                  text, re.I)
    if m:
        try:
            yr = int(m.group(3))
            if yr < 100:
                yr += 2000
            return datetime.date(yr, int(m.group(1)), int(m.group(2))).isoformat()
        except Exception:
            pass
    return ""


def fetch_rhino_jazz():
    """Scrape the Acoustic Sounds Rhino Hi-Fi list; keep JAZZ vinyl only."""
    listing = _get_html(RHINO_AS["listing"])
    # Unique product detail pages on the listing: /d/<id>/<slug>
    detail = {}
    for m in re.finditer(r"/d/(\d+)/([A-Za-z0-9_%./&'\-]+)", listing):
        detail.setdefault(m.group(1), f"{RHINO_AS['base']}/d/{m.group(1)}/{m.group(2)}")

    items = []
    for pid, url in list(detail.items())[:60]:  # bound the work
        try:
            html = _get_html(url)
        except Exception:
            continue
        # Must actually be a Rhino High Fidelity title (guards against the
        # store's "top sellers" sidebar leaking unrelated products in).
        if RHINO_AS["label_path"] not in html:
            continue
        # Genre lives in the product table:  Genre: ... /g/<id>/<Name>
        gm = re.search(r"Genre:.*?/g/\d+/([^\"'\s<]+)", html, re.S | re.I)
        genre = (gm.group(1) if gm else "").replace("_", " ")
        if "jazz" not in genre.lower():
            continue  # skip rock / pop / etc.
        tm = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
        raw = (tm.group(1) if tm else "").split("|")[0].strip()
        title = re.sub(r"\s*-\s*[^-]*(?:Vinyl Record|Vinyl|LP|SACD|CD)[^-]*$",
                       "", raw).strip() or raw
        am = re.search(r"Availability:\s*(?:</?[^>]+>\s*)*([A-Za-z][A-Za-z /\-]+)",
                       html, re.I)
        avail = am.group(1).strip() if am else ""
        pm = re.search(r"\$\s?(\d+\.\d{2})", html)
        items.append({
            "id": f"rhinoas_{pid}",
            "title": title,
            "url": url,
            "price": pm.group(1) if pm else None,
            "image": None,
            "published_at": _parse_release_date(html),
            "availability": avail,
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
# AI consensus blurbs  (optional; only runs if ANTHROPIC_API_KEY is set)
# ---------------------------------------------------------------------------
# For each release we ask Claude for a one-line audiophile-reception summary.
# This is the model's synthesized take on the title/pressing reputation, NOT a
# live scrape of any forum -- so it's labelled "AI summary" in the app. Blurbs
# are cached by release id in blurbs.json so we only pay for each title once.
def load_blurbs():
    if BLURB_FILE.exists():
        try:
            return json.loads(BLURB_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_blurbs(b):
    BLURB_FILE.write_text(json.dumps(b, indent=2, ensure_ascii=False))


def ai_blurb(title, label):
    """One short consensus line for a release, or '' if AI isn't configured."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return ""
    prompt = (
        "You are summarizing the general audiophile/critical reception of a "
        "specific jazz vinyl reissue, for a collector deciding whether to buy. "
        f"Release: \"{title}\" ({label}). "
        "In ONE sentence (max 30 words), give the consensus on this reissue's "
        "sound/pressing reputation and how it ranks among its series. Base it on "
        "your general knowledge of these audiophile series and this album's "
        "reputation. If you don't have specific information, say so briefly "
        "rather than inventing detail. No preamble, just the sentence."
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": ANTHROPIC_MODEL,
                "max_tokens": 120,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
        return " ".join(parts).strip()
    except Exception as e:
        print(f"  AI blurb failed for {title!r}: {e}")
        return ""


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
    blurbs_changed = False

    # Shopify-based label feeds.
    for src in SOURCES:
        print(f"Checking {src['label']} ...")
        try:
            raw = fetch_products(src)
        except Exception as e:
            print(f"  ERROR fetching {src['id']}: {e}")
            continue
        items = {it["id"]: it for it in (simplify(src, p) for p in raw)}
        print(f"  found {len(items)} products")
        for it in items.values():
            it["label_name"] = src["label"]
            catalog.append(it)
        if diff_source(state, src["id"], src["label"], items, all_new):
            changed = True

    # Rhino High Fidelity (jazz only) -- scraped from Acoustic Sounds.
    print(f"Checking {RHINO_AS['label']} ...")
    try:
        ritems = {it["id"]: it for it in fetch_rhino_jazz()}
        print(f"  found {len(ritems)} jazz products")
        for it in ritems.values():
            it["label_name"] = RHINO_AS["label"]
            catalog.append(it)
        if diff_source(state, RHINO_AS["id"], RHINO_AS["label"], ritems, all_new):
            changed = True
    except Exception as e:
        print(f"  ERROR fetching {RHINO_AS['id']}: {e}")

    # Attach AI consensus blurbs (cached per id, so each title is paid for once).
    if os.environ.get("ANTHROPIC_API_KEY"):
        print("Generating AI blurbs for any new titles ...")
        for it in catalog:
            bid = it["id"]
            if bid in blurbs:
                it["blurb"] = blurbs[bid]
            else:
                text = ai_blurb(it["title"], it.get("label_name", ""))
                blurbs[bid] = text
                it["blurb"] = text
                blurbs_changed = True
        if blurbs_changed:
            save_blurbs(blurbs)
    else:
        print("ANTHROPIC_API_KEY not set -> skipping AI blurbs.")
        for it in catalog:
            it["blurb"] = blurbs.get(it["id"], "")

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
