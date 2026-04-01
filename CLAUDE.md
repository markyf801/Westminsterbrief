# Westminster Brief — Project Instructions

## What this project is
Westminster Brief (`westminsterbrief.co.uk`) is an AI-powered parliamentary research tool built for UK government officials. It lets users search Hansard, track Written Questions, analyse debates, and generate Word briefings. Deployed on Railway.

## Stack
- **Backend:** Flask 3.0 with blueprints, deployed on Railway
- **Database:** SQLite locally → PostgreSQL on Railway (auto-switched via `DATABASE_URL` env var)
- **AI:** Google Gemini API (`google-genai`, model: `gemini-1.5-flash` and `gemini-embedding-001`)
- **Frontend:** Jinja2 templates + vanilla JS, static CSS at `static/style.css`
- **Auth:** Flask-Login with werkzeug password hashing
- **Exports:** python-docx for Word document generation

## Project structure
```
flask_app.py          Main app: config, DB models, auth routes, alerts scanner, blueprint registration
hansard.py            Blueprint: Written Questions search & export (route: /)
tracker.py            Blueprint: Today's PQs + AI categorisation (route: /tracker)
mp_search.py          Blueprint: MP/Peer PQ research (route: /mp_search)
biography.py          Blueprint: MP/Lords biography with AI summary (route: /biography)
debate_scanner.py     Blueprint: Debate search, transcript scraping, AI briefing (route: /debates)
templates/            Jinja2 HTML templates — base.html is the master layout
static/style.css      All CSS — no inline styles in base.html or index.html
```

## External APIs used
| API | Env var | Used for |
|-----|---------|----------|
| Google Gemini | `GEMINI_API_KEY` | AI summaries, embeddings, categorisation |
| They Work For You | `TWFY_API_KEY` | Debate transcripts, Hansard search |
| News API | `NEWS_API_KEY` | Media scan in Smart Alerts |
| Bluesky | `BSKY_HANDLE` + `BSKY_PASSWORD` | Stakeholder social monitoring |
| Parliament API | none (public) | Written Questions, MP/member data |

## Database models (flask_app.py)
- `User` — email + hashed password
- `TrackedTopic` — keyword + department, belongs to User
- `TrackedStakeholder` — name + Bluesky handle, belongs to User
- `Alert` — result from AI scan, linked to Topic or Stakeholder

## Deployment (Railway)
- Entry point: `gunicorn flask_app:app` (see `Procfile` and `railway.toml`)
- Add a **PostgreSQL plugin** in Railway — it sets `DATABASE_URL` automatically
- Set all env vars in Railway dashboard (see API table above)
- Also set `SECRET_KEY` to a long random string in Railway env vars
- Domain: westminsterbrief.co.uk — point GoDaddy DNS to Railway after deploy

## Environment variables needed on Railway
```
SECRET_KEY=<long random string>
GEMINI_API_KEY=
TWFY_API_KEY=
NEWS_API_KEY=
BSKY_HANDLE=
BSKY_PASSWORD=
DATABASE_URL=<set automatically by Railway PostgreSQL plugin>
```

## Local development
```bash
cd c:\Users\marky\hansard_app
pip install -r requirements.txt
python flask_app.py
```
App runs at http://127.0.0.1:5000 — visit /home for the dashboard.

## Known issues / tech debt
- Written Questions search can be slow — Parliament API latency, no caching yet
- The `SECRET_KEY` in flask_app.py is a placeholder — must be overridden by env var on Railway
- Backup files in root (bckup_flask.py etc.) and backup templates are clutter — safe to delete eventually
- No database migration system — relies on `db.create_all()` which is fine for now

## Things to avoid
- Don't use port 5432 for Supabase if ever added — use the connection pooler on 6543
- Don't hardcode API keys or .env paths
- Don't use Flask dev server in production (`debug=True` is only active when running locally via `__main__`)
