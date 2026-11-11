# IG Automation – setup

1. developers.facebook.com → Create App → type **Business** → add product **Instagram** → "API setup with Instagram login".
2. Copy **Instagram App ID / Secret** into `.env`.
3. Valid OAuth Redirect URI: `BASE_URL/callback`
4. Webhooks: Callback URL `BASE_URL/webhook`, Verify token = `VERIFY_TOKEN`. Subscribe to `comments` and `messages`.
5. Run: `pip install -r requirements.txt && export $(cat .env | xargs) && python app.py`
   (needs a public HTTPS URL – use ngrok locally, Render/Railway in production)
6. Open `BASE_URL` → Login with Instagram → dashboard.

Notes
- Account must be Instagram **Business/Creator**.
- Dev mode: only users added as app testers can log in. To let *anyone* log in, submit the app for **Meta App Review** (permissions: instagram_business_basic, _manage_comments, _manage_messages, _content_publish) + Business Verification.
- DMs: Instagram allows one private reply per comment (within 7 days) and free-form DMs only within 24h of the user's last message.
- Post images must be a public JPEG URL.

## v2 additions
- Analytics tab (7-day chart, totals, top keywords) + CSV export
- Tokens encrypted at rest (Fernet, key derived from SECRET_KEY)
- Postgres: set `DATABASE_URL` (SQLite otherwise)
- Rules: multiple keywords, random reply variants, `{username}`, per-post targeting, on/off toggle
- 24h DM cooldown per person + webhook de-duplication
- Failed posts: retry/delete
- `/privacy`, `/data-deletion`, and "Disconnect & delete my data" (needed for Meta App Review)
- Run a single gunicorn worker (see Procfile) so the scheduler doesn't double-post
