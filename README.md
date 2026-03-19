# PulseCheck

> Real-time keyword sentiment analysis powered by Twitter/X, Google News, and VADER NLP — with a live global news globe.

![Python](https://img.shields.io/badge/python-3.12-blue?style=flat-square)
![Flask](https://img.shields.io/badge/flask-3.0-lightgrey?style=flat-square)
![PocketBase](https://img.shields.io/badge/pocketbase-0.36-orange?style=flat-square)
![License](https://img.shields.io/badge/license-MIT-green?style=flat-square)

---

## Screenshots

<!-- Drop your screenshots here -->
| Main Dashboard | Globe View |
|---|---|
| _screenshot coming soon_ | _screenshot coming soon_ |

---

## What it does

PulseCheck has two pages:

**`/` — Sentiment Analyzer**
Type any keyword, hashtag, or phrase. PulseCheck fetches recent tweets or Google News headlines and scores each one using VADER NLP — returning a live sentiment verdict (Positive / Negative / Neutral), compound score, word cloud, score distribution chart, and per-item cards with animated score bars.

**`/globe` — World News Globe**
A rotating 3D globe (globe.gl) showing live Google News sentiment by country. Click any country to see regional headlines. The right panel auto-scrolls through articles and appends new ones as they arrive — no duplicates. Drag the divider to resize the panel.

---

## Features

### Analyzer (`/`)
- **Dual source toggle** — switch between Twitter/X and Google News (English) per search
- **Hybrid Twitter fetcher** — TwitterAPI.io primary, twikit scraping fallback (no official API key needed for fallback)
- **VADER NLP** — compound score, positive/negative/neutral per item
- **Animated verdict** — typewriter word, breathing compound meter, particle effects
- **Chart/meter toggle** — switch between the needle gauge and a bar chart
- **Word cloud** — positive and negative word clouds side by side
- **Score bars** — animated fill bar on every tweet/article card
- **Heat-tint cards** — card background tinted by compound intensity
- **History sidebar** — full PocketBase-backed history with per-record delete
- **Heatmap calendar** — GitHub-style activity heatmap from saved analyses
- **Recent chips** — last 8 searches, source-aware (GNews chip re-opens as GNews)
- **Cooldown bar** — prevents hammering the same keyword
- **Toast notifications** — non-blocking error and success messages
- **Dark / light theme** — persisted in localStorage

### Globe (`/globe`)
- **Rotating globe** — auto-rotates when idle, pauses on interaction
- **Country points** — colored green/red/amber after sentiment fetch
- **Country click** → live regional Google News headlines with mini verdict
- **Global news feed** — auto-scrolling article list, pauses on hover
- **Append-only dedup** — only new unseen articles are added; no repeats
- **Fetch skeleton** — shimmer card animation while loading next batch
- **Resizable panel** — drag the divider, globe redraws in real time
- **Reset button** — return to global rotating view from any country
- **News ticker** — scrolling horizontal headline strip

### Shared
- **PocketBase persistence** — analyses tagged with `source` field (twitter / google-news)
- **In-memory cache** — per-keyword result cache, configurable TTL
- **Rate limiting** — per-IP, per-keyword cooldown on the backend
- **Single shared CSS** — `style.css` with design tokens used by both pages

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | [Flask](https://flask.palletsprojects.com/) 3.0 + httpx |
| Twitter (primary) | [TwitterAPI.io](https://twitterapi.io) — paid, higher limits |
| Twitter (fallback) | [twikit](https://github.com/d60/twikit) — cookie-based scraping, free |
| News source | [GNews](https://github.com/ranahaani/GNews) — Google News RSS, no key needed |
| NLP | [VADER](https://github.com/cjhutto/vaderSentiment) — social-media-tuned sentiment |
| Database | [PocketBase](https://pocketbase.io/) — single-binary SQLite backend |
| Globe | [globe.gl](https://globe.gl/) — WebGL 3D globe |
| Frontend | Vanilla HTML / CSS / JS — no build step, no framework |
| Fonts | Cormorant Garamond + JetBrains Mono (Google Fonts) |

---

## Project Structure

```
pulsecheck/
├── backend/
│   ├── app.py              # Flask server — all routes and logic
│   ├── pb_setup.py         # One-time PocketBase collection setup
│   └── requirements.txt
├── frontend/
│   ├── index.html          # Sentiment analyzer dashboard
│   ├── globe.html          # World news globe
│   └── style.css           # Shared design tokens and base styles
├── pb_migrations/          # PocketBase schema migrations
└── pb_data/                # PocketBase database (do not commit)
```

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/yourname/pulsecheck.git
cd pulsecheck
pip install -r backend/requirements.txt
```

### 2. Configure environment

Create `backend/.env`:

```env
# Twitter — one or both
TWITTERAPI_IO_KEY=your_key_here          # optional but preferred
COOKIES_FILE=cookies.json                # for twikit fallback

# PocketBase
POCKETBASE_URL=http://127.0.0.1:8090
POCKETBASE_EMAIL=admin@example.com
POCKETBASE_PASSWORD=yourpassword

# Server
PORT=5000
FLASK_DEBUG=false
```

### 3. Twitter cookies (twikit fallback)

If you don't have a TwitterAPI.io key, twikit uses your browser session — no developer account needed.

1. Log in to [x.com](https://x.com) in Chrome or Firefox
2. Install [Cookie-Editor](https://cookie-editor.com/)
3. On x.com → open Cookie-Editor → **Export as JSON**
4. Save as `backend/cookies.json`

> Your `cookies.json` contains your session token. Never commit it or share it.

### 4. Start PocketBase

```bash
# Download (Linux x64 example)
wget https://github.com/pocketbase/pocketbase/releases/download/v0.36.5/pocketbase_0.36.5_linux_amd64.zip
unzip pocketbase_0.36.5_linux_amd64.zip

# Start
./pocketbase serve

# Visit http://127.0.0.1:8090/_/ and create your superuser account
# Then run the setup script once:
python backend/pb_setup.py
```

PocketBase is optional — the app works without it, history just won't be saved.

### 5. Run

```bash
python backend/app.py
```

Open [http://localhost:5000](http://localhost:5000) for the analyzer, [http://localhost:5000/globe](http://localhost:5000/globe) for the globe.

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/analyze` | Fetch and score tweets or news articles |
| `GET` | `/api/history` | List saved analyses from PocketBase |
| `DELETE` | `/api/history/:id` | Delete a single analysis |
| `GET` | `/api/globe?country=us` | Regional news + sentiment for a country code |
| `GET` | `/api/globe?country=world` | Global top headlines |
| `GET` | `/api/health` | Backend + PocketBase health check |

**POST `/api/analyze` body:**
```json
{
  "keyword": "climate",
  "count": 20,
  "source": "twitter"
}
```
`source` is `"twitter"` or `"news"`. `count` is 10–100.

---

## How VADER scoring works

| Compound score | Label |
|---|---|
| ≥ +0.05 | Positive |
| ≤ −0.05 | Negative |
| Between | Neutral |

VADER is tuned for social media — it handles slang, caps, punctuation, and emoji. The compound score runs from **−1.0** (most negative) to **+1.0** (most positive).

> **English only.** VADER scores non-English text near 0.0 regardless of actual sentiment. For multilingual support, consider `cardiffnlp/twitter-xlm-roberta-base-sentiment`.

---

## Known Limitations

- **twikit** returns ~20–40 tweets per search (one page of Twitter's internal API). Cookie sessions expire — re-export from browser when they do.
- **GNews** is English only and scrapes Google News RSS. Results depend on what Google surfaces.
- **VADER** is English-only — non-English tweets will score near neutral.
- **Globe sentiment** is based on English news headlines for each country, not native-language sources.

---

## Security

Add to `.gitignore`:

```
backend/.env
backend/cookies.json
backend/cookies_twikit.json
pb_data/
```

Never expose `cookies.json` — it holds your Twitter session. Regenerate by logging out and back in if leaked.

---

## Possible next additions

- [ ] Keyword comparison — sentiment of two keywords side by side
- [ ] Trend chart — compound score over time from history data
- [ ] Export — download results as CSV or PNG
- [ ] Multilingual sentiment — swap VADER for a multilingual model
- [ ] Deploy guide — Railway / Render / VPS one-click setup

---

## Credits

PulseCheck is built on the shoulders of these open-source projects and services:

| Project | What it does here |
|---|---|
| [twikit](https://github.com/d60/twikit) by [@d60](https://github.com/d60) | Cookie-based Twitter scraping — no official API key needed |
| [VADER Sentiment](https://github.com/cjhutto/vaderSentiment) by C.J. Hutto & E. Gilbert | NLP engine that scores every tweet and headline |
| [GNews](https://github.com/ranahaani/GNews) by [@ranahaani](https://github.com/ranahaani) | Google News RSS fetching — powers the news source toggle and the globe |
| [globe.gl](https://github.com/vasturiano/globe.gl) by [@vasturiano](https://github.com/vasturiano) | WebGL 3D globe rendering in the `/globe` page |
| [PocketBase](https://github.com/pocketbase/pocketbase) | Single-binary SQLite backend for analysis history |
| [Flask](https://github.com/pallets/flask) | Python web framework powering the API |
| [Cormorant Garamond](https://fonts.google.com/specimen/Cormorant+Garamond) | Editorial serif typeface |
| [JetBrains Mono](https://www.jetbrains.com/lp/mono/) | Monospace font used throughout the UI |

---

## License

MIT — see [LICENSE](LICENSE).
