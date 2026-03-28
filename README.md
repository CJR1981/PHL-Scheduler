# PHL Catering Scheduler

American Airlines · Philadelphia International Airport  
Flask + OR-Tools CP-SAT · Supabase · Render.com

---

## Files in this repo

| File | Purpose |
|------|---------|
| `app.py` | Flask API server — all routes |
| `scheduler_engine.py` | CP-SAT scheduling algorithm |
| `live_ops.py` | Delay, reassign, sick-call live ops |
| `impact_engine.py` | Delay/gate-change impact analysis |
| `recovery_engine.py` | Post-disruption recovery options |
| `equity_engine.py` | Agent workload equity tracking |
| `export.py` | Excel export (9-tab workbook) |
| `auth.py` | Token auth + role-based access |
| `schedule_store.py` | Schedule persistence helpers |
| `supabase_client.py` | Supabase connection wrapper |
| `agent.py` | AI agent (Anthropic API) |
| `create_admin.py` | One-time admin user setup |
| `static/index.html` | Single-page frontend |
| `requirements.txt` | Python dependencies |
| `render.yaml` | Render deploy blueprint |
| `runtime.txt` | Python version pin |

---

## Deploying: GitHub then Render

### Step 1 — Create a GitHub repository

1. Go to https://github.com → **New repository**
2. Name it `phl-catering-scheduler`
3. Set it to **Private**
4. Do **not** tick "Add a README" (you already have one)
5. Click **Create repository**

### Step 2 — Push the code

Open Terminal in this folder and run:

```bash
git init
git add .
git commit -m "Initial deploy"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/phl-catering-scheduler.git
git push -u origin main
```

Replace `YOUR_USERNAME` with your GitHub username.

### Step 3 — Connect to Render

1. Go to https://render.com → **New** → **Web Service**
2. Connect your GitHub account
3. Select the `phl-catering-scheduler` repo
4. Render auto-detects `render.yaml` → click **Apply**
5. If it doesn't auto-detect, set manually:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app --workers 2 --timeout 180 --bind 0.0.0.0:$PORT`

### Step 4 — Set environment variables in Render

In your Render service → **Environment**, add:

| Key | Value |
|-----|-------|
| `PYTHON_VERSION` | `3.11.9` |
| `SUPABASE_URL` | Your Supabase project URL |
| `SUPABASE_KEY` | Your Supabase anon key |
| `ANTHROPIC_API_KEY` | Your Anthropic API key |

**Never commit real keys to GitHub — always use Render's Environment panel.**

### Step 5 — Deploy

Click **Deploy**. Build takes 2–4 minutes.  
Your app will be live at `https://phl-catering-scheduler.onrender.com`

---

## Updating the app

```bash
git add .
git commit -m "Description of change"
git push
```

Render auto-deploys on every push to `main`.

---

## First-time admin setup

Run once after deploying to create the admin account:

```bash
export SUPABASE_URL="https://xxxx.supabase.co"
export SUPABASE_KEY="your-anon-key"
python create_admin.py
```

---

## Local development

```bash
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

export SUPABASE_URL="https://xxxx.supabase.co"
export SUPABASE_KEY="your-anon-key"
export ANTHROPIC_API_KEY="your-key"

python app.py
# Runs at http://localhost:5050
```

---

## Notes

- **Python 3.11.9 is required** — ortools is incompatible with Python 3.12+
- Render free tier spins down after 15 min idle; first request takes ~30s to wake
- `--timeout 180` handles longer CP-SAT solve times on large schedules
