# Jazz Vinyl Reissue Monitor

Een autonome agent die dagelijks de officiële labelwinkels controleert op nieuwe
jazz-reissues op vinyl en je een seintje stuurt zodra er iets nieuws verschijnt.
Draait gratis op GitHub Actions — geen server nodig, niets dat jij draaiend moet houden.

## Welke labels worden gevolgd

| Label | Serie | Bron |
|------|------|------|
| Blue Note | Tone Poet Series | store.bluenote.com (Shopify-feed) |
| Blue Note | Classic Vinyl Series | store.bluenote.com (Shopify-feed) |
| Craft Recordings | OJC Series | craftrecordings.com (Shopify-feed) |
| Verve | Acoustic Sounds Series | store.ververecords.com (Shopify-feed) |
| Rhino | High Fidelity Series | rhino.com (vooral rock; jazz is zeldzaam) |

De vier jazz-series zijn per definitie jazz, dus elke nieuwe titel telt mee.
Rhino is grotendeels rock en heel laag in volume (~2 per kwartaal), dus daar
worden alle nieuwe Hi-Fi-titels gemeld en kies jij zelf wat interessant is.

## Hoe werkt het

Bijna alle audiophile reissue-labels draaien hun webshop op Shopify, en Shopify
geeft standaard een nette JSON-feed van producten. Het script leest die feed per
label, vergelijkt met wat het vorige keer zag (opgeslagen in `seen.json`) en
meldt alleen wat echt nieuw is. De eerste run is een **basismeting**: die legt
vast wat er nu staat zonder je te spammen. Vanaf de tweede run krijg je alleen
echte toevoegingen.

Als een Shopify-collectie ooit van naam verandert, valt het script automatisch
terug op de winkel-brede feed en filtert het op trefwoord — zo breekt de monitor
niet stil.

## Opzetten (eenmalig, ~10 min)

### 1. Maak een repo
- Maak een nieuwe (private) GitHub-repo aan.
- Zet deze bestanden erin:
  - `monitor.py`
  - `requirements.txt`
  - `monitor.yml` → verplaats naar `.github/workflows/monitor.yml`

> Accounts aanmaken en inloggen moet je zelf doen — dat kan ik om
> veiligheidsredenen niet voor je doen.

### 2. Kies één notificatiekanaal en zet de secret(s)
Ga in je repo naar **Settings → Secrets and variables → Actions → New repository secret**
en vul in wat bij jouw gekozen kanaal hoort. Je hebt er maar één nodig.

**Discord (eenvoudigst):**
- `DISCORD_WEBHOOK_URL` — in Discord: kanaalinstellingen → Integrations →
  Webhooks → New Webhook → Copy Webhook URL. Geen bot of token nodig.

**Telegram:**
- `TELEGRAM_BOT_TOKEN` — via @BotFather een bot aanmaken.
- `TELEGRAM_CHAT_ID` — stuur je bot een bericht, open dan
  `https://api.telegram.org/bot<TOKEN>/getUpdates` en lees je chat-id af.

**E-mail (SMTP):**
- `SMTP_HOST`, `SMTP_PORT` (meestal 465), `SMTP_USER`, `SMTP_PASS`, `EMAIL_TO`.
- Bij Gmail: gebruik een **app-wachtwoord**, niet je gewone wachtwoord.

### 3. Testen
- Ga naar het tabblad **Actions** → "Jazz Vinyl Monitor" → **Run workflow**.
- De eerste run legt de basismeting vast (geen melding — dat is normaal).
- Bekijk het log: je ziet per label hoeveel producten gevonden zijn.
  - Staat er bij Rhino een fout of 0? Pas dan alleen de `base`/`collection`
    voor `rhino_hifi` boven in `monitor.py` aan. De rest is geverifieerd.
- Draai 'm daarna nog eens (of wacht op de dagelijkse cron) om de meldingen te testen.

### 4. Schema aanpassen (optioneel)
In `.github/workflows/monitor.yml` staat `cron: "0 8 * * *"` (dagelijks 08:00 UTC).
Wil je vaker checken? Bijv. twee keer per dag: `cron: "0 8,20 * * *"`.

## Onderhoud
Weinig. Het enige dat ooit stuk kan gaan is als een label z'n winkel verbouwt;
dan klopt een collectie-handle niet meer. De fallback vangt de meeste gevallen
op; anders is het één regel aanpassen in `SOURCES` boven in `monitor.py`.
