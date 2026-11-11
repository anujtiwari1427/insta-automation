import os, csv, io, hmac, hashlib, base64, random, requests, datetime as dt
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
from urllib.parse import urlencode
from flask import Flask, request, redirect, session, render_template_string, abort, jsonify, Response
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import create_engine, text
from cryptography.fernet import Fernet

APP_ID = os.environ.get("IG_APP_ID", "27956043357410206")
APP_SECRET = os.environ.get("IG_APP_SECRET", "6247202a696622a355faa26f81a51cd0")
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
SECRET_KEY = os.environ.get("SECRET_KEY", "coded-decoded-secret-key-2026-secure")
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "my_secret_verify_token")
G = os.environ.get("GRAPH_API_BASE", "https://graph.facebook.com/v21.0")
SCOPES = ",".join(["instagram_business_basic", "instagram_business_manage_comments",
                   "instagram_business_manage_messages", "instagram_business_content_publish"])

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config.update(SESSION_COOKIE_SECURE=BASE_URL.startswith("https"), SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

# Vercel Serverless Path Normalizer
class VercelPathFix:
    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app
    def __call__(self, environ, start_response):
        environ['DEBUG_ORIG_PATH_INFO'] = environ.get('PATH_INFO', '')
        # If HTTP_X_MATCHED_PATH is present, what is it?
        # Let's preserve the original path unless it starts with /api/
        path = environ.get('PATH_INFO', '')
        for prefix in ['/api/index.py', '/api/index']:
            if path.startswith(prefix):
                environ['PATH_INFO'] = path[len(prefix):] or '/'
                break
        return self.wsgi_app(environ, start_response)

app.wsgi_app = VercelPathFix(app.wsgi_app)

# ---------- DB (SQLite in /tmp for Vercel, Postgres via DATABASE_URL) ----------
is_serverless = bool(
    os.environ.get("VERCEL")
    or os.environ.get("VERCEL_ENV")
    or os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
    or os.environ.get("LAMBDA_TASK_ROOT")
    or not os.access(".", os.W_OK)
)

raw_url = os.environ.get("DATABASE_URL", "")
if raw_url:
    raw_url = raw_url.replace("postgres://", "postgresql://")
    if is_serverless and raw_url.startswith("sqlite") and not raw_url.startswith("sqlite:////tmp/"):
        URL = "sqlite:////tmp/data.db"
    else:
        URL = raw_url
else:
    URL = "sqlite:////tmp/data.db" if is_serverless else "sqlite:///data.db"

eng = create_engine(
    URL,
    connect_args={"check_same_thread": False} if URL.startswith("sqlite") else {},
    pool_pre_ping=True
)
PK = "serial primary key" if URL.startswith("postgresql") else "integer primary key autoincrement"

def q(sql, one=False, **a):
    with eng.begin() as c:
        r = c.execute(text(sql), a)
        if not r.returns_rows: return None
        rows = [dict(x._mapping) for x in r]
        return (rows[0] if rows else None) if one else rows

# ---------- helpers ----------
F = Fernet(base64.urlsafe_b64encode(hashlib.sha256(SECRET_KEY.encode()).digest()))
enc = lambda s: F.encrypt(s.encode()).decode()
dec = lambda s: F.decrypt(s.encode()).decode()
now = lambda: dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

_db_ready = False
def init_db():
    global _db_ready
    if _db_ready: return
    try:
        for s in [
            "create table if not exists users(ig_id text primary key, username text, token text)",
            f"create table if not exists rules(id {PK}, ig_id text, keyword text, media_id text, comment_reply text, dm_text text, active integer default 1)",
            f"create table if not exists posts(id {PK}, ig_id text, image_url text, caption text, run_at text, status text default 'pending')",
            f"create table if not exists logs(id {PK}, ig_id text, ts text, kind text, keyword text, who text, ref text, msg text)"
        ]:
            q(s)

        # Seed user if environment variables are set
        ig_id = os.environ.get("IG_BUSINESS_ID")
        tok = os.environ.get("PAGE_ACCESS_TOKEN")
        if ig_id and tok:
            try:
                q("""insert into users(ig_id,username,token) values(:i,:u,:t)
                     on conflict(ig_id) do update set username=:u, token=:t""",
                  i=ig_id, u="coded.ecoded", t=enc(tok))
            except Exception as ex:
                print("User seed notice:", ex)

        # Seed default rule if none exist
        if ig_id:
            try:
                existing = q("select count(*) n from rules", one=True)
                if existing and existing.get("n", 0) == 0:
                    q("""insert into rules(ig_id,keyword,media_id,comment_reply,dm_text,active)
                         values(:i,:k,:m,:c,:d,1)""",
                      i=ig_id,
                      k="price, link, info, details, send",
                      m="",
                      c="Sent you a DM! Check your inbox 🚀\nJust sent you the link in DM! 📩",
                      d="Hey {username}! Thanks for reaching out.\nHere is the link: https://instagram.com/coded.ecoded\nLet us know if you need anything else! ✨")
            except Exception as ex:
                print("Rule seed notice:", ex)

        _db_ready = True
    except Exception as e:
        print("Database init notice:", e)

init_db()

@app.before_request
def check_db():
    init_db()

# ---------- helpers ----------
def log(ig, kind, msg, keyword="", who="", ref=""):
    q("insert into logs(ig_id,ts,kind,keyword,who,ref,msg) values(:i,:t,:k,:kw,:w,:r,:m)",
      i=ig, t=now(), k=kind, kw=keyword, w=who, r=ref, m=msg)

def api(url, tok, **kw):
    try: return requests.post(url, params={"access_token": tok}, timeout=20, **kw).json()
    except Exception as e: return {"error": str(e)}

def fill(s, name):
    opts = [x.strip() for x in (s or "").split("\n") if x.strip()]
    return random.choice(opts).replace("{username}", "@" + name) if opts else ""

def me():
    if "ig" not in session: abort(401)
    u = q("select * from users where ig_id=:i", i=session["ig"], one=True)
    if not u: session.clear(); abort(401)
    return u

import traceback

@app.errorhandler(Exception)
def handle_exception(e):
    return jsonify({
        "error": "Unhandled Exception",
        "type": type(e).__name__,
        "message": str(e),
        "traceback": traceback.format_exc()
    }), 500

@app.errorhandler(404)
def not_found(e):
    return jsonify({
        "error": "404 Not Found",
        "path": request.path,
        "full_path": request.full_path,
        "url": request.url,
        "environ_path": request.environ.get("PATH_INFO")
    }), 404

# ---------- auth: login = automatic token ----------
@app.route("/")
def home():
    if request.args.get("debug"):
        return jsonify({k: str(v) for k, v in request.environ.items()})
    ig_id = os.environ.get("IG_BUSINESS_ID")
    if "ig" not in session and ig_id:
        tok = os.environ.get("PAGE_ACCESS_TOKEN", "")
        if tok:
            try:
                q("""insert into users(ig_id,username,token) values(:i,:u,:t)
                     on conflict(ig_id) do update set username=:u, token=:t""",
                  i=ig_id, u="coded.ecoded", t=enc(tok))
            except Exception as ex:
                print("User upsert notice:", ex)
        session["ig"] = ig_id
    return redirect("/dashboard") if "ig" in session else render_template_string(PAGE, user=None)

@app.route("/login")
def login():
    return redirect("https://www.instagram.com/oauth/authorize?" + urlencode({
        "client_id": APP_ID, "redirect_uri": BASE_URL + "/callback", "response_type": "code", "scope": SCOPES}))

@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code: return "Login cancelled", 400
    t = requests.post("https://api.instagram.com/oauth/access_token", data={
        "client_id": APP_ID, "client_secret": APP_SECRET, "grant_type": "authorization_code",
        "redirect_uri": BASE_URL + "/callback", "code": code}).json()
    if "access_token" not in t: return "Login failed", 400
    ll = requests.get("https://graph.instagram.com/access_token", params={
        "grant_type": "ig_exchange_token", "client_secret": APP_SECRET, "access_token": t["access_token"]}).json()
    tok = ll.get("access_token", t["access_token"])
    p = requests.get(f"{G}/me", params={"fields": "user_id,username", "access_token": tok}).json()
    ig = str(p["user_id"])
    q("""insert into users(ig_id,username,token) values(:i,:u,:t)
         on conflict(ig_id) do update set username=:u, token=:t""", i=ig, u=p.get("username"), t=enc(tok))
    api(f"{G}/{ig}/subscribed_apps", tok, data={"subscribed_fields": "comments,messages"})
    session["ig"] = ig
    return redirect("/dashboard")

@app.route("/logout")
def logout(): session.clear(); return redirect("/")

@app.route("/disconnect")
def disconnect():
    u = me()
    for t in ["logs", "posts", "rules", "users"]: q(f"delete from {t} where ig_id=:i", i=u["ig_id"])
    session.clear(); return redirect("/")

@app.route("/privacy")
@app.route("/data-deletion")
def legal():
    return ("<body style='font-family:system-ui;max-width:640px;margin:auto;padding:20px'><h2>Privacy & data deletion</h2>"
            "<p>We store your Instagram username, an encrypted access token, and your automation rules and activity logs, "
            "only to run the automations you set up. We never sell data.</p>"
            f"<p>To delete all your data: log in and click <b>Disconnect &amp; delete my data</b> in the dashboard, or visit <a href='{BASE_URL}/disconnect'>{BASE_URL}/disconnect</a> while logged in.</p></body>")

# ---------- dashboard + analytics ----------
@app.route("/dashboard")
def dashboard():
    if request.args.get("debug"):
        return jsonify({
            "status": "reached dashboard successfully",
            "path": request.path,
            "PATH_INFO": request.environ.get("PATH_INFO"),
            "session": dict(session)
        })
    u = me(); i = u["ig_id"]
    return render_template_string(PAGE, user=u,
        rules=q("select * from rules where ig_id=:i order by id desc", i=i),
        posts=q("select * from posts where ig_id=:i order by run_at desc", i=i),
        logs=q("select * from logs where ig_id=:i order by id desc limit 50", i=i))

@app.route("/api/stats")
def stats():
    i = me()["ig_id"]
    days = [(dt.datetime.utcnow() - dt.timedelta(days=n)).strftime("%Y-%m-%d") for n in range(6, -1, -1)]
    rows = q("select substr(ts,1,10) d, kind, count(*) n from logs where ig_id=:i and ts>=:s group by substr(ts,1,10), kind", i=i, s=days[0])
    series = {k: [next((r["n"] for r in rows if r["d"] == d and r["kind"] == k), 0) for d in days] for k in ["comment", "dm", "post", "error"]}
    tot = {r["kind"]: r["n"] for r in q("select kind, count(*) n from logs where ig_id=:i group by kind", i=i)}
    top = q("select keyword, count(*) n from logs where ig_id=:i and keyword<>'' group by keyword order by n desc limit 5", i=i)
    return jsonify(days=[d[5:] for d in days], series=series, totals=tot, top=top)

@app.route("/export.csv")
def export():
    i = me()["ig_id"]; out = io.StringIO(); w = csv.writer(out); w.writerow(["time_utc", "type", "who", "keyword", "message"])
    for r in q("select * from logs where ig_id=:i order by id", i=i): w.writerow([r["ts"], r["kind"], r["who"], r["keyword"], r["msg"]])
    return Response(out.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=activity.csv"})

# ---------- rules + posts CRUD ----------
@app.route("/rules", methods=["POST"])
def add_rule():
    u = me(); f = request.form
    q("insert into rules(ig_id,keyword,media_id,comment_reply,dm_text) values(:i,:k,:m,:c,:d)",
      i=u["ig_id"], k=f["keyword"].lower().strip(), m=f["media_id"].strip(), c=f["comment_reply"], d=f["dm_text"])
    return redirect("/dashboard")

@app.route("/rules/<int:r>/<act>")
def rule_act(r, act):
    i = me()["ig_id"]
    if act == "delete": q("delete from rules where id=:r and ig_id=:i", r=r, i=i)
    elif act == "toggle": q("update rules set active=1-active where id=:r and ig_id=:i", r=r, i=i)
    return redirect("/dashboard")

@app.route("/schedule", methods=["POST"])
def schedule():
    u = me(); f = request.form
    q("insert into posts(ig_id,image_url,caption,run_at) values(:i,:u,:c,:t)",
      i=u["ig_id"], u=f["image_url"], c=f["caption"], t=f["run_at"].replace("T", " ") + ":00")
    return redirect("/dashboard")

@app.route("/posts/<int:p>/<act>")
def post_act(p, act):
    i = me()["ig_id"]
    if act == "delete": q("delete from posts where id=:p and ig_id=:i", p=p, i=i)
    elif act == "retry": q("update posts set status='pending' where id=:p and ig_id=:i", p=p, i=i)
    return redirect("/dashboard")

# ---------- webhook: auto reply + DM ----------
def match(ig, txt, media):
    txt = (txt or "").lower()
    for r in q("select * from rules where ig_id=:i and active=1 order by id", i=ig):
        if r["media_id"] and r["media_id"] != media: continue
        ks = [k.strip() for k in (r["keyword"] or "").split(",") if k.strip()]
        hit = next((k for k in ks if k in txt), None)
        if not ks or hit: return r, (hit or "")
    return None, ""

def sent_dm_recently(ig, who):
    since = (dt.datetime.utcnow() - dt.timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    return q("select 1 x from logs where ig_id=:i and kind='dm' and who=:w and ts>=:s", i=ig, w=who, s=since, one=True)

def on_comment(u, v):
    ig, tok = u["ig_id"], dec(u["token"]); uid = str(v["from"]["id"]); name = v["from"].get("username", "")
    if uid == ig or q("select 1 x from logs where ig_id=:i and kind='comment' and ref=:r", i=ig, r=v["id"], one=True): return
    r, kw = match(ig, v.get("text"), (v.get("media") or {}).get("id"))
    if not r: return
    if r["comment_reply"]:
        res = api(f"{G}/{v['id']}/replies", tok, data={"message": fill(r["comment_reply"], name)})
        log(ig, "comment" if "id" in res else "error", f"Reply to @{name}" + ("" if "id" in res else f" failed: {res}"), kw, uid, v["id"])
    else: log(ig, "comment", f"Matched @{name}", kw, uid, v["id"])
    if r["dm_text"] and not sent_dm_recently(ig, uid):
        res = api(f"{G}/{ig}/messages", tok, json={"recipient": {"comment_id": v["id"]}, "message": {"text": fill(r["dm_text"], name)}})
        log(ig, "dm" if "message_id" in res else "error", f"DM to @{name}" + ("" if "message_id" in res else f" failed: {res}"), kw, uid)

def on_message(u, m):
    ig, tok = u["ig_id"], dec(u["token"]); sid = str(m["sender"]["id"]); msg = m.get("message", {})
    if sid == ig or msg.get("is_echo") or sent_dm_recently(ig, sid): return
    r, kw = match(ig, msg.get("text"), None)
    if r and r["dm_text"]:
        res = api(f"{G}/{ig}/messages", tok, json={"recipient": {"id": sid}, "message": {"text": fill(r["dm_text"], "")}})
        log(ig, "dm" if "message_id" in res else "error", "Auto-replied to inbound DM", kw, sid)

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        if request.args.get("hub.verify_token") == VERIFY_TOKEN: return request.args.get("hub.challenge", "")
        abort(403)
    exp = "sha256=" + hmac.new(APP_SECRET.encode(), request.data, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(request.headers.get("X-Hub-Signature-256", ""), exp): abort(403)
    for e in (request.json or {}).get("entry", []):
        u = q("select * from users where ig_id=:i", i=str(e["id"]), one=True)
        if not u: continue
        for ch in e.get("changes", []):
            if ch.get("field") == "comments": on_comment(u, ch["value"])
        for m in e.get("messaging", []): on_message(u, m)
    return "ok"

# ---------- background jobs ----------
def publish_due():
    for p in q("select * from posts where status='pending' and run_at<=:n", n=now()):
        u = q("select * from users where ig_id=:i", i=p["ig_id"], one=True)
        if not u: continue
        tok = dec(u["token"])
        c = api(f"{G}/{p['ig_id']}/media", tok, data={"image_url": p["image_url"], "caption": p["caption"]})
        r = api(f"{G}/{p['ig_id']}/media_publish", tok, data={"creation_id": c["id"]}) if "id" in c else c
        ok = "id" in r
        q("update posts set status=:s where id=:i", s="posted" if ok else "failed", i=p["id"])
        log(p["ig_id"], "post" if ok else "error", f"Post {'published' if ok else 'failed: ' + str(r)[:120]}")

def refresh_tokens():
    for u in q("select * from users"):
        r = requests.get("https://graph.instagram.com/refresh_access_token",
                         params={"grant_type": "ig_refresh_token", "access_token": dec(u["token"])}).json()
        if "access_token" in r: q("update users set token=:t where ig_id=:i", t=enc(r["access_token"]), i=u["ig_id"])

sch = BackgroundScheduler()
if not is_serverless:
    sch.add_job(publish_due, "interval", minutes=1)
    sch.add_job(refresh_tokens, "interval", days=30)
    try:
        sch.start()
    except Exception as e:
        print("Scheduler notice:", e)

PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1"><title>IG Automation</title>
<style>body{font-family:system-ui;max-width:820px;margin:auto;padding:16px;background:#0f0f14;color:#eee}
input,textarea,button{width:100%;padding:10px;margin:4px 0;border-radius:8px;border:1px solid #333;background:#1a1a22;color:#eee;box-sizing:border-box;font:inherit}
button,.btn{background:#e1306c;border:0;font-weight:600;cursor:pointer;color:#fff;padding:12px;border-radius:8px;text-decoration:none;display:inline-block}
.card{background:#16161d;padding:14px;border-radius:12px;margin:12px 0}small,.m{color:#999}a{color:#f77}
nav{display:flex;gap:6px;overflow:auto}nav a{padding:8px 14px;background:#1a1a22;border-radius:20px;text-decoration:none;color:#eee;white-space:nowrap}
.g{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}.g div{background:#1a1a22;border-radius:10px;padding:10px;text-align:center}.g b{font-size:22px;display:block}
.tab{display:none}.tab.on{display:block}.row{display:flex;justify-content:space-between;gap:8px;border-bottom:1px solid #222;padding:6px 0}</style>
{% if not user %}<h1>📲 IG Automation</h1><p>Auto comment replies, auto DMs, scheduled posts and analytics.</p>
<a class=btn href=/login>Login with Instagram</a><p><small><a href=/privacy>Privacy</a></small></p>
{% else %}<h2>@{{user.username}} <small><a href=/logout>logout</a></small></h2>
<nav><a href=#o>Overview</a><a href=#r>Rules</a><a href=#p>Posts</a><a href=#a>Activity</a></nav>
<div class=tab id=o><div class=card><div class=g><div><b id=t1>0</b>Comments</div><div><b id=t2>0</b>DMs</div><div><b id=t3>0</b>Posts</div><div><b id=t4>0</b>Errors</div></div></div>
<div class=card><canvas id=ch height=160></canvas></div><div class=card><b>Top keywords</b><div id=top class=m>–</div></div></div>
<div class=tab id=r><div class=card><h3>New rule</h3><small>Keywords: comma-separated, empty = any. Replies: one variant per line (random pick), use {username}.</small>
<form method=post action=/rules><input name=keyword placeholder="Keywords e.g. price, link, info"><input name=media_id placeholder="Only for post ID (optional)">
<textarea name=comment_reply placeholder="Public reply variants"></textarea><textarea name=dm_text placeholder="DM variants (optional)"></textarea><button>Add rule</button></form></div>
<div class=card>{% for r in rules %}<div class=row><span>{{'🟢' if r.active else '⚪'}} <b>{{r.keyword or '*any*'}}</b> {{('· post '+r.media_id) if r.media_id}}<br><small>{{(r.comment_reply or '')[:60]}} | DM: {{(r.dm_text or '')[:40]}}</small></span>
<span><a href=/rules/{{r.id}}/toggle>toggle</a> <a href=/rules/{{r.id}}/delete>✕</a></span></div>{% else %}<small>No rules yet.</small>{% endfor %}</div></div>
<div class=tab id=p><div class=card><h3>Schedule a post</h3><form method=post action=/schedule><input name=image_url placeholder="Public JPEG URL">
<textarea name=caption placeholder=Caption></textarea><input type=datetime-local name=run_at required><small>Time in UTC</small><button>Schedule</button></form></div>
<div class=card>{% for p in posts %}<div class=row><span>{{p.run_at[:16]}} {{p.caption[:36]}} <small>[{{p.status}}]</small></span><span>{% if p.status=='failed' %}<a href=/posts/{{p.id}}/retry>retry</a> {% endif %}<a href=/posts/{{p.id}}/delete>✕</a></span></div>{% else %}<small>Nothing scheduled.</small>{% endfor %}</div></div>
<div class=tab id=a><div class=card><a href=/export.csv>⬇ Export CSV</a>{% for l in logs %}<div class=row><span>{{'⚠️ ' if l.kind=='error'}}{{l.msg}}</span><small>{{l.ts[5:16]}}</small></div>{% endfor %}</div></div>
<p><small><a href=/disconnect onclick="return confirm('Delete all your data?')">Disconnect &amp; delete my data</a> · <a href=/privacy>Privacy</a></small></p>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<script>
function tab(){var h=(location.hash||'#o').slice(1);document.querySelectorAll('.tab').forEach(t=>t.classList.toggle('on',t.id==h))}
addEventListener('hashchange',tab);tab();
fetch('/api/stats').then(r=>r.json()).then(d=>{
 ['comment','dm','post','error'].forEach((k,i)=>document.getElementById('t'+(i+1)).textContent=d.totals[k]||0);
 document.getElementById('top').innerHTML=d.top.map(x=>x.keyword+' <b>'+x.n+'</b>').join(' · ')||'–';
 new Chart(document.getElementById('ch'),{type:'bar',data:{labels:d.days,datasets:[
  {label:'Comments',data:d.series.comment,backgroundColor:'#e1306c'},{label:'DMs',data:d.series.dm,backgroundColor:'#833ab4'},{label:'Posts',data:d.series.post,backgroundColor:'#fcb045'}]}});
});</script>{% endif %}"""

if __name__ == "__main__":
    app.run(port=5000)
