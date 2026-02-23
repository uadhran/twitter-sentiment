# PulseCheck — Twitter Keyword Sentiment Analyzer

A web dashboard that fetches recent tweets for any keyword using **twikit** (no API key needed) and scores them using **VADER** sentiment analysis. Results are optionally persisted to **PocketBase** for history tracking.

![PulseCheck Dashboard](https://img.shields.io/badge/status-working-brightgreen) ![Python](https://img.shields.io/badge/python-3.12-blue) ![Flask](https://img.shields.io/badge/flask-3.0-lightgrey) ![PocketBase](https://img.shields.io/badge/pocketbase-0.36-orange)

---

## ✨ Features

- 🔍 **Keyword search** — fetch up to 40 recent tweets per query
- 🧠 **VADER sentiment scoring** — positive / negative / neutral with compound score
- 🌐 **Non-English detection** — flags tweets where VADER may be inaccurate
- 🔗 **Clickable tweet links** — every tweet links directly to the original on X
- 📊 **Sentiment distribution bar** — visual breakdown at a glance
- 🗂️ **PocketBase history** — every analysis saved, browsable in the sidebar
- 📰 **Editorial UI** — clean newspaper-style dashboard, no build step needed

---

## 📁 Project Structure

```
twitter-sentiment/
├── backend/
│   ├── app.py              # Flask API server (twikit + VADER + PocketBase)
│   ├── pb_setup.py         # One-time PocketBase collection setup script
│   ├── requirements.txt    # Python dependencies
│   ├── .env.example        # Environment variable template
│   ├── .env                # Your credentials (never commit!)
│   ├── cookies.json        # Exported from browser via Cookie-Editor
│   └── cookies_twikit.json # Auto-generated converted format
└── frontend/
    └── index.html          # Dashboard (open directly in browser, no build)
```

---

## ⚙️ Setup

### 1. Install dependencies

```bash
cd backend
pip install -r requirements.txt
```

---

### 2. Get Twitter cookies (no API key needed)

twikit uses your regular Twitter/X login session — no developer account required.

1. Log in to [x.com](https://x.com) in your browser
2. Install the **Cookie-Editor** extension:
   - [Chrome](https://chrome.google.com/webstore/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm)
   - [Firefox](https://addons.mozilla.org/en-US/firefox/addon/cookie-editor/)
3. On x.com, click the extension → **Export** → **Export as JSON**
4. Save the file as `backend/cookies.json`

> ⚠️ Your `cookies.json` contains your session token — never share it or commit it to git.

---

### 3. Configure `.env`

```bash
cp .env.example .env
```

Edit `.env`:

```env
# Path to your exported cookies file
COOKIES_FILE=cookies.json

# PocketBase (optional — for history saving)
POCKETBASE_URL=http://127.0.0.1:8090
POCKETBASE_EMAIL=your_superuser@email.com
POCKETBASE_PASSWORD=your_superuser_password
```

---

### 4. (Optional) Set up PocketBase for history

PocketBase is a single-file open source backend that saves all your analyses.

```bash
# Download PocketBase for Linux x64
wget https://github.com/pocketbase/pocketbase/releases/download/v0.36.5/pocketbase_0.36.5_linux_amd64.zip
unzip pocketbase_0.36.5_linux_amd64.zip

# Start PocketBase
./pocketbase serve

# Visit http://127.0.0.1:8090/_/ and create your superuser account
# Then add credentials to your .env file and run the setup script:

cd backend
python pb_setup.py
```

This creates two collections automatically:
- **`analyses`** — keyword, sentiment summary, percentages, timestamp
- **`tweets`** — individual tweets linked to each analysis

> PocketBase is completely optional. If not running, the app works normally — history just won't be saved.

---

### 5. Run the backend

```bash
cd backend
python app.py
# or with uv:
uv run python3 -u app.py
```

Server starts at `http://localhost:5000`

On first run, `cookies.json` is auto-converted to `cookies_twikit.json` (twikit's format).

---

### 6. Open the dashboard

Open `frontend/index.html` directly in your browser:

```
file:///home/youruser/twitter-sentiment/frontend/index.html
```

No build step, no npm, no bundler needed.

---

## 🚀 Usage

1. Start PocketBase (optional): `./pocketbase serve`
2. Start Flask: `python backend/app.py`
3. Open `frontend/index.html`
4. Type any keyword, hashtag, or phrase and press **Analyze →**
5. Results show:
   - **Verdict banner** — overall sentiment + compound score
   - **Distribution bar** — visual positive/negative/neutral split
   - **Tweet table** — per-tweet labels, scores, engagement, timestamps
   - **Clickable timestamps** — links to original tweet on X
   - **Non-English warnings** — flagged rows where scoring may be unreliable
   - **Sidebar history** — past analyses from PocketBase, click to re-run

---

## 🧠 How VADER Scoring Works

VADER (Valence Aware Dictionary and sEntiment Reasoner) is specifically tuned for social media text.

| Compound Score | Classification |
|----------------|----------------|
| ≥ +0.05        | Positive ✅    |
| ≤ −0.05        | Negative ❌    |
| Between        | Neutral ➡️     |

The compound score ranges from **−1.0** (most negative) to **+1.0** (most positive).

> **Non-English limitation:** VADER is trained on English text only. Tweets in Hindi, Malayalam, Arabic, CJK scripts etc. will typically score near 0.000 (neutral) even if they carry strong sentiment. These are automatically flagged with a `🌐 non-english · VADER may be inaccurate` badge. For multilingual support, consider `cardiffnlp/twitter-xlm-roberta-base-sentiment`.

---

## 🛠️ Tech Stack

| Layer     | Technology |
|-----------|-----------|
| Scraping  | [twikit](https://github.com/d60/twikit) — no API key, uses browser cookies |
| NLP       | [VADER](https://github.com/cjhutto/vaderSentiment) — fast, social-media-tuned |
| Backend   | [Flask](https://flask.palletsprojects.com/) + httpx |
| Database  | [PocketBase](https://pocketbase.io/) — single-file SQLite backend |
| Frontend  | Vanilla HTML/CSS/JS — no framework, no build step |
| Fonts     | Playfair Display + DM Sans + DM Mono (Google Fonts) |

---

## 🔒 Security Notes

Add these to your `.gitignore`:

```
backend/.env
backend/cookies.json
backend/cookies_twikit.json
pocketbase/pb_data/
```

- **Never share your `cookies.json`** — it contains your Twitter session token
- **Regenerate your session** (log out + back in) if you accidentally expose it
- PocketBase `pb_data/` contains your database — back it up, don't commit it

---

## 📌 Known Limitations

- twikit returns ~20–40 tweets per search (one page of Twitter's internal API)
- Rate limits apply — avoid running many searches in rapid succession
- Cookie sessions expire periodically — re-export from browser when they do
- VADER is English-only — non-English tweets are flagged but scores are unreliable
>>>>>>> 73812ee (Initial commit)
