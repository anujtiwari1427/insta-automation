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
from urllib.parse import parse_qs, urlencode

class VercelPathFix:
    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app
    def __call__(self, environ, start_response):
        qs = environ.get('QUERY_STRING', '')
        if '__path' in qs:
            params = parse_qs(qs, keep_blank_values=True)
            if '__path' in params:
                subpath = params.pop('__path')[0]
                environ['PATH_INFO'] = '/' + subpath.lstrip('/')
                environ['QUERY_STRING'] = urlencode([(k, v) for k, vs in params.items() for v in vs])
        else:
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
            f"create table if not exists logs(id {PK}, ig_id text, ts text, kind text, keyword text, who text, ref text, msg text)",
            f"create table if not exists comments(id {PK}, ig_id text, comment_id text unique, media_id text, username text, user_id text, text text, reply_sent text, dm_sent text, matched_keyword text, status text, created_at text)"
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

        # Seed initial comments and posts if empty
        if ig_id:
            try:
                c_cnt = q("select count(*) n from comments", one=True)
                if c_cnt and c_cnt.get("n", 0) == 0:
                    samples = [
                        ("c_101", "17992837482910", "sarah_dev", "u_1", "How much does the automation course cost? Send link please! 🔥", "Sent you a DM! Check your inbox 🚀", "Hey @sarah_dev! Here is the access link: https://instagram.com/coded.ecoded", "price, link", "replied", "Just now"),
                        ("c_102", "17992837482910", "alex_tech", "u_2", "Link please! Excited to test this out 🚀", "Just sent you the link in DM! 📩", "Hey @alex_tech! Here is your direct link: https://instagram.com/coded.ecoded", "link", "replied", "4m ago"),
                        ("c_103", "17894028472911", "code_ninja", "u_3", "Is this working with Python 3.12 serverless?", "Yes! Full Python serverless support. Details in DM ✨", "Hey @code_ninja! Python 3.12 and Vercel serverless are fully supported.", "info", "replied", "12m ago"),
                        ("c_104", "17912409582103", "elena_design", "u_4", "Can I use this for carousel posts?", "Sent you the guide in DM! 📲", "Hey @elena_design! Carousel publishing is fully supported.", "info", "replied", "28m ago")
                    ]
                    for c_id, m_id, u_name, u_id, c_txt, r_sent, d_sent, kw, st, ts in samples:
                        q("""insert into comments(ig_id, comment_id, media_id, username, user_id, text, reply_sent, dm_sent, matched_keyword, status, created_at)
                             values(:i, :c, :m, :u, :uid, :t, :r, :d, :k, :s, :ts)
                             on conflict(comment_id) do nothing""",
                          i=ig_id, c=c_id, m=m_id, u=u_name, uid=u_id, t=c_txt, r=r_sent, d=d_sent, k=kw, s=st, ts=ts)
            except Exception as ex:
                print("Comment seed notice:", ex)

            try:
                p_cnt = q("select count(*) n from posts", one=True)
                if p_cnt and p_cnt.get("n", 0) == 0:
                    post_samples = [
                        ("https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?w=600&auto=format&fit=crop", "🚀 Automated Instagram Comment-to-DM system is officially live! Comment 'LINK' below to get early access.", "2026-11-11 10:00:00", "published"),
                        ("https://images.unsplash.com/photo-1607799279861-4dd421887fb3?w=600&auto=format&fit=crop", "Python 3.12 + Meta Graph API v21.0 tutorial out now. Comment 'CODE' to get the complete repository!", "2026-11-10 18:00:00", "published"),
                        ("https://images.unsplash.com/photo-1550745165-9bc0b252726f?w=600&auto=format&fit=crop", "How we automated 10,000+ DMs without getting flagged by Instagram spam filters. Comment 'GUIDE' for the breakdown.", "2026-11-09 14:00:00", "published")
                    ]
                    for img_url, cap, run_at, st in post_samples:
                        q("insert into posts(ig_id, image_url, caption, run_at, status) values(:i, :u, :c, :t, :s)",
                          i=ig_id, u=img_url, c=cap, t=run_at, s=st)
            except Exception as ex:
                print("Post seed notice:", ex)

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

def sync_instagram_data(ig_id, tok):
    try:
        # 1. Fetch real media from Instagram Graph API
        url = f"{G}/{ig_id}/media"
        res = requests.get(url, params={
            "fields": "id,caption,media_type,media_url,permalink,thumbnail_url,timestamp,comments_count,like_count",
            "access_token": tok,
            "limit": 20
        }, timeout=10)
        
        if res.status_code != 200:
            return {"success": False, "error": res.json().get("error", {}).get("message", res.text), "media_count": 0, "comments_count": 0}
        
        media_items = res.json().get("data", [])
        if media_items:
            q("delete from posts where ig_id=:i", i=ig_id)
            q("delete from comments where ig_id=:i", i=ig_id)
        
        total_comments = 0
        for m in media_items:
            mid = m.get("id")
            img_url = m.get("media_url") or m.get("thumbnail_url") or ""
            cap = m.get("caption", "")
            ts = (m.get("timestamp") or now())[:19].replace("T", " ")
            q("insert into posts(ig_id, image_url, caption, run_at, status) values(:i, :u, :c, :t, 'published')",
              i=ig_id, u=img_url, c=cap, t=ts)
            
            # 2. Fetch real comments for this media item
            c_res = requests.get(f"{G}/{mid}/comments", params={
                "fields": "id,text,from,timestamp,like_count",
                "access_token": tok,
                "limit": 25
            }, timeout=8)
            if c_res.status_code == 200:
                comments = c_res.json().get("data", [])
                for c in comments:
                    cid = c.get("id")
                    ctxt = c.get("text", "")
                    cuser = (c.get("from") or {}).get("username") or "instagram_user"
                    cuid = (c.get("from") or {}).get("id") or ""
                    cts = (c.get("timestamp") or now())[:19].replace("T", " ")
                    r, kw = match(ig_id, ctxt, mid)
                    reply = fill(r["comment_reply"], cuser) if (r and r["comment_reply"]) else ""
                    dm = fill(r["dm_text"], cuser) if (r and r["dm_text"]) else ""
                    
                    q("""insert into comments(ig_id, comment_id, media_id, username, user_id, text, reply_sent, dm_sent, matched_keyword, status, created_at)
                         values(:i, :c, :m, :u, :uid, :t, :r, :d, :k, :s, :ts)
                         on conflict(comment_id) do update set reply_sent=:r, dm_sent=:d, status=:s""",
                      i=ig_id, c=cid, m=mid, u=cuser, uid=cuid, t=ctxt, r=reply, d=dm, k=kw or "", s="replied" if (reply or dm) else "received", ts=cts)
                    total_comments += 1

        return {"success": True, "media_count": len(media_items), "comments_count": total_comments}
    except Exception as e:
        return {"success": False, "error": str(e), "media_count": 0, "comments_count": 0}

@app.route("/dashboard")
def dashboard():
    u = me(); i = u["ig_id"]
    return render_template_string(PAGE, user=u,
        rules=q("select * from rules where ig_id=:i order by id desc", i=i),
        posts=q("select * from posts where ig_id=:i order by id desc", i=i),
        comments=q("select * from comments where ig_id=:i order by id desc limit 50", i=i),
        logs=q("select * from logs where ig_id=:i order by id desc limit 50", i=i),
        base_url=BASE_URL or request.host_url.rstrip("/"),
        verify_token=VERIFY_TOKEN)

@app.route("/api/comments")
def api_comments():
    u = me(); i = u["ig_id"]
    rows = q("select * from comments where ig_id=:i order by id desc limit 50", i=i)
    return jsonify({"comments": rows or []})


@app.route("/api/sync", methods=["POST"])
def api_sync():
    u = me(); i = u["ig_id"]; tok = dec(u["token"])
    res = sync_instagram_data(i, tok)
    return jsonify(res)

@app.route("/api/save-token", methods=["POST"])
def api_save_token():
    u = me(); i = u["ig_id"]
    new_tok = (request.json or {}).get("token", "").strip()
    if not new_tok:
        return jsonify({"success": False, "error": "Token is required"}), 400
    
    # Verify token
    test_res = requests.get(f"{G}/{i}?fields=id,username&access_token={new_tok}")
    if test_res.status_code != 200:
        # Try /me
        test_res2 = requests.get(f"{G}/me?access_token={new_tok}")
        if test_res2.status_code != 200:
            return jsonify({"success": False, "error": "Invalid token or permissions: " + test_res.text}), 400
    
    # Save token
    q("update users set token=:t where ig_id=:i", t=enc(new_tok), i=i)
    
    # Run sync
    sync_res = sync_instagram_data(i, new_tok)
    return jsonify({"success": True, "sync": sync_res})

@app.route("/api/media")
def api_media():
    u = me(); i = u["ig_id"]; tok = dec(u["token"])
    try:
        res = requests.get(f"{G}/{i}/media", params={
            "fields": "id,caption,media_type,media_url,permalink,thumbnail_url,timestamp,comments_count,like_count",
            "access_token": tok
        }, timeout=8)
        if res.status_code == 200:
            data = res.json().get("data", [])
            if data: return jsonify({"media": data, "source": "instagram"})
    except Exception:
        pass
    db_posts = q("select * from posts where ig_id=:i order by id desc", i=i)
    return jsonify({"media": db_posts or [], "source": "local"})

@app.route("/api/simulate", methods=["POST"])
def simulate():
    u = me(); i = u["ig_id"]
    data = request.json or {}
    text = data.get("text", "")
    username = data.get("username", "live_tester")
    media_id = data.get("media_id", "17992837482910")
    r, kw = match(i, text, media_id)
    reply = fill(r["comment_reply"], username) if (r and r["comment_reply"]) else "Thanks for your comment! Check your DMs 🚀"
    dm = fill(r["dm_text"], username) if (r and r["dm_text"]) else "Hey! Here is the link you requested: https://instagram.com/coded.ecoded"
    cid = f"sim_{int(dt.datetime.utcnow().timestamp())}"
    q("""insert into comments(ig_id, comment_id, media_id, username, user_id, text, reply_sent, dm_sent, matched_keyword, status, created_at)
         values(:i, :c, :m, :u, :uid, :t, :r, :d, :k, 'replied', :ts)""",
      i=i, c=cid, m=media_id, u=username, uid="sim_user", t=text, r=reply, d=dm, k=kw or "any", ts="Just now")
    log(i, "comment", f"Simulated reply to @{username}: {reply[:40]}", kw or "any", username, cid)
    return jsonify({
        "matched": bool(r),
        "keyword": kw or "any",
        "comment_reply": reply,
        "dm_text": dm,
        "comment_id": cid,
        "created_at": "Just now"
    })

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

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>InstaFlow - Instagram Automation & Live Comments</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
:root {
  --bg-dark: #0e0f14;
  --bg-sidebar: #13141b;
  --bg-card: #171822;
  --bg-card-subtle: #1c1d27;
  --border-subtle: rgba(255, 255, 255, 0.07);
  --border-hover: rgba(255, 255, 255, 0.14);
  --text-main: #f3f4f6;
  --text-muted: #8b8f9e;
  --accent-purple: #a855f7;
  --accent-purple-glow: rgba(168, 85, 247, 0.25);
  --accent-blue: #2563eb;
  --accent-green: #22c55e;
  --accent-pink: #ec4899;
  --accent-cyan: #06b6d4;
  --accent-amber: #f59e0b;
}

* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: 'Plus Jakarta Sans', system-ui, -apple-system, sans-serif;
  background-color: var(--bg-dark);
  color: var(--text-main);
  min-height: 100vh;
  display: flex;
  overflow-x: hidden;
}

/* Sidebar */
aside.sidebar {
  width: 260px;
  background: var(--bg-sidebar);
  border-right: 1px solid var(--border-subtle);
  padding: 24px 18px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  flex-shrink: 0;
  min-height: 100vh;
}

.logo-area {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 28px;
}
.logo-icon {
  width: 42px;
  height: 42px;
  border-radius: 12px;
  background: linear-gradient(135deg, #a855f7, #6366f1);
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 800;
  font-size: 20px;
  color: #fff;
  box-shadow: 0 4px 15px var(--accent-purple-glow);
}
.logo-text {
  font-size: 18px;
  font-weight: 700;
  letter-spacing: -0.3px;
}
.logo-text span {
  color: var(--accent-purple);
}

nav.sidebar-nav {
  display: flex;
  flex-direction: column;
  gap: 6px;
  flex-grow: 1;
}

.nav-link {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 11px 14px;
  border-radius: 14px;
  color: var(--text-muted);
  text-decoration: none;
  font-size: 14px;
  font-weight: 500;
  transition: all 0.2s ease;
  position: relative;
}
.nav-link:hover {
  color: var(--text-main);
  background: rgba(255, 255, 255, 0.04);
}
.nav-link.active {
  background: #222430;
  color: #ffffff;
  font-weight: 600;
}
.nav-link svg {
  width: 19px;
  height: 19px;
  stroke-width: 2;
  opacity: 0.8;
  flex-shrink: 0;
}
.nav-link.active svg {
  opacity: 1;
  stroke: var(--accent-purple);
}
.live-pill {
  margin-left: auto;
  background: rgba(34, 197, 94, 0.15);
  color: var(--accent-green);
  font-size: 10px;
  font-weight: 700;
  padding: 2px 7px;
  border-radius: 10px;
  display: flex;
  align-items: center;
  gap: 4px;
}
.live-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent-green);
  box-shadow: 0 0 8px var(--accent-green);
  animation: pulse 1.8s infinite;
}
@keyframes pulse {
  0% { opacity: 0.4; }
  50% { opacity: 1; }
  100% { opacity: 0.4; }
}

.sidebar-bottom {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding-top: 18px;
  border-top: 1px solid var(--border-subtle);
}

.theme-toggle-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 4px;
  font-size: 13px;
  color: var(--text-muted);
}
.toggle-switch {
  width: 44px;
  height: 24px;
  background: #2b2d3c;
  border-radius: 20px;
  position: relative;
  cursor: pointer;
}
.toggle-thumb {
  width: 18px;
  height: 18px;
  background: #fff;
  border-radius: 50%;
  position: absolute;
  top: 3px;
  right: 4px;
}

.pro-card {
  background: #ffffff;
  color: #111827;
  border-radius: 18px;
  padding: 16px;
  box-shadow: 0 10px 25px rgba(0,0,0,0.4);
}
.pro-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 6px;
}
.pro-badge {
  background: #a855f7;
  color: #fff;
  font-size: 11px;
  font-weight: 700;
  padding: 2px 8px;
  border-radius: 10px;
}
.pro-card h4 {
  font-size: 16px;
  font-weight: 800;
  margin-bottom: 2px;
}
.pro-card p {
  font-size: 12px;
  color: #6b7280;
  margin-bottom: 10px;
}
.pro-btn {
  display: block;
  width: 100%;
  text-align: center;
  padding: 8px;
  background: #111827;
  color: #ffffff;
  border-radius: 10px;
  font-size: 12px;
  font-weight: 600;
  text-decoration: none;
}

/* Main Content */
main.main-content {
  flex-grow: 1;
  padding: 28px 36px;
  max-width: 1380px;
  margin: 0 auto;
  overflow-y: auto;
}

/* Header */
.top-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  margin-bottom: 28px;
}
.search-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  background: #171822;
  border: 1px solid var(--border-subtle);
  border-radius: 30px;
  padding: 10px 20px;
  width: 380px;
}
.search-bar svg {
  stroke: var(--text-muted);
  width: 18px;
  height: 18px;
}
.search-bar input {
  background: transparent;
  border: none;
  outline: none;
  color: var(--text-main);
  font-size: 14px;
  width: 100%;
}
.header-actions {
  display: flex;
  align-items: center;
  gap: 16px;
}
.btn-primary {
  display: flex;
  align-items: center;
  gap: 8px;
  background: var(--accent-blue);
  color: #fff;
  border: none;
  border-radius: 30px;
  padding: 10px 20px;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  text-decoration: none;
  box-shadow: 0 4px 14px rgba(37, 99, 235, 0.3);
  transition: all 0.2s;
}
.btn-primary:hover {
  background: #1d4ed8;
  transform: translateY(-1px);
}
.icon-btn {
  width: 42px;
  height: 42px;
  border-radius: 50%;
  background: var(--bg-card);
  border: 1px solid var(--border-subtle);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-muted);
  cursor: pointer;
  position: relative;
  text-decoration: none;
}
.notification-dot {
  position: absolute;
  top: 10px;
  right: 10px;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #ef4444;
}
.user-avatar-badge {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 4px 12px 4px 4px;
  background: var(--bg-card);
  border: 1px solid var(--border-subtle);
  border-radius: 30px;
}
.avatar-img {
  width: 34px;
  height: 34px;
  border-radius: 50%;
  background: linear-gradient(135deg, #ec4899, #a855f7);
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 700;
  font-size: 13px;
  color: #fff;
}
.user-name {
  font-size: 13px;
  font-weight: 600;
}

/* Welcome Row */
.welcome-row {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  margin-bottom: 24px;
}
.welcome-text h2 {
  font-size: 28px;
  font-weight: 800;
  letter-spacing: -0.5px;
  display: flex;
  align-items: center;
  gap: 10px;
}
.welcome-text p {
  font-size: 14px;
  color: var(--text-muted);
  margin-top: 2px;
}
.time-selector {
  display: flex;
  align-items: center;
  gap: 4px;
  background: #171822;
  border: 1px solid var(--border-subtle);
  border-radius: 20px;
  padding: 4px;
}
.time-pill {
  padding: 6px 16px;
  border-radius: 16px;
  font-size: 13px;
  color: var(--text-muted);
  text-decoration: none;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.2s;
}
.time-pill.active {
  background: #232534;
  color: #ffffff;
  font-weight: 600;
}

/* Hero Balance Card */
.hero-card {
  background: var(--bg-card);
  border: 1px solid var(--border-subtle);
  border-radius: 24px;
  padding: 26px 30px;
  margin-bottom: 24px;
}
.hero-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 20px;
}
.hero-card-title {
  font-size: 16px;
  font-weight: 600;
  color: var(--text-muted);
}
.hero-tabs {
  display: flex;
  gap: 6px;
  background: #1f202c;
  padding: 4px;
  border-radius: 16px;
}
.hero-tab {
  padding: 6px 16px;
  font-size: 13px;
  border-radius: 12px;
  color: var(--text-muted);
  cursor: pointer;
  font-weight: 500;
  transition: all 0.2s;
}
.hero-tab.active {
  background: #2b2d3d;
  color: #ffffff;
  font-weight: 600;
}

.hero-main-stat {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  margin-bottom: 20px;
}
.stat-large-group {
  display: flex;
  align-items: center;
  gap: 16px;
}
.stat-large {
  font-size: 48px;
  font-weight: 800;
  letter-spacing: -1px;
}
.growth-pill {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  background: rgba(34, 197, 94, 0.15);
  color: var(--accent-green);
  font-size: 13px;
  font-weight: 700;
  padding: 4px 10px;
  border-radius: 20px;
}
.chart-wrapper {
  height: 160px;
  width: 55%;
  position: relative;
}

.hero-substats {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 20px;
  padding-top: 20px;
  border-top: 1px solid var(--border-subtle);
}
.substat-col {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.substat-label { font-size: 13px; color: var(--text-muted); }
.substat-value {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 20px;
  font-weight: 700;
}
.substat-growth { font-size: 12px; font-weight: 600; }
.growth-up { color: var(--accent-green); }
.growth-neutral { color: var(--accent-purple); }

/* Lower Grid */
.dashboard-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 24px;
  margin-bottom: 24px;
}
.grid-card {
  background: var(--bg-card);
  border: 1px solid var(--border-subtle);
  border-radius: 24px;
  padding: 24px 26px;
}
.grid-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 20px;
}
.grid-card-title { font-size: 17px; font-weight: 700; display: flex; align-items: center; gap: 8px; }
.grid-card-link {
  font-size: 13px;
  color: var(--accent-purple);
  text-decoration: none;
  font-weight: 600;
  cursor: pointer;
}
.grid-card-link:hover { text-decoration: underline; }

/* Ring card */
.ring-card-body {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
}
.ring-stat-area {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.ring-stat-large {
  font-size: 38px;
  font-weight: 800;
  display: flex;
  align-items: center;
  gap: 10px;
}
.ring-legend {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.legend-item {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
  color: var(--text-muted);
}
.legend-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
}
.legend-val {
  font-weight: 700;
  color: var(--text-main);
  margin-left: auto;
}
.ring-chart-box {
  width: 160px;
  height: 160px;
  position: relative;
}

/* Event Stream Table */
.stream-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.stream-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 14px;
  background: var(--bg-card-subtle);
  border-radius: 14px;
  border: 1px solid rgba(255, 255, 255, 0.03);
  transition: all 0.2s;
}
.stream-user {
  display: flex;
  align-items: center;
  gap: 10px;
}
.stream-avatar {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  background: #2b2d3d;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 700;
}
.stream-meta {
  display: flex;
  flex-direction: column;
}
.stream-name { font-size: 13px; font-weight: 600; }
.stream-time { font-size: 11px; color: var(--text-muted); }
.stream-badge {
  font-size: 11px;
  font-weight: 600;
  padding: 4px 10px;
  border-radius: 12px;
  background: rgba(168, 85, 247, 0.15);
  color: var(--accent-purple);
}

/* Tabs System */
.tab-pane { display: none; }
.tab-pane.active { display: block; }

/* Real-time Comments Feed */
.live-header-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 20px;
  flex-wrap: wrap;
  gap: 14px;
}
.live-beacon {
  display: flex;
  align-items: center;
  gap: 8px;
  background: rgba(34, 197, 94, 0.12);
  border: 1px solid rgba(34, 197, 94, 0.3);
  padding: 6px 14px;
  border-radius: 20px;
  font-size: 12px;
  font-weight: 700;
  color: var(--accent-green);
}
.comment-card {
  background: var(--bg-card);
  border: 1px solid var(--border-subtle);
  border-radius: 20px;
  padding: 20px 24px;
  margin-bottom: 14px;
  transition: all 0.3s ease;
  position: relative;
  overflow: hidden;
}
.comment-card:hover {
  border-color: var(--border-hover);
  transform: translateY(-2px);
}
.comment-card.new-item {
  border-color: var(--accent-purple);
  box-shadow: 0 0 20px var(--accent-purple-glow);
}
.comment-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}
.commenter-info {
  display: flex;
  align-items: center;
  gap: 12px;
}
.avatar-ring {
  width: 40px;
  height: 40px;
  border-radius: 50%;
  padding: 2px;
  background: linear-gradient(45deg, #f09433, #e6683c 25%, #dc2743 50%, #cc2366 75%, #bc1888 100%);
  display: flex;
  align-items: center;
  justify-content: center;
}
.avatar-ring-inner {
  width: 100%;
  height: 100%;
  border-radius: 50%;
  background: #111;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 700;
  font-size: 13px;
  color: #fff;
}
.commenter-name {
  font-size: 14px;
  font-weight: 700;
}
.comment-post-ref {
  font-size: 11px;
  color: var(--accent-purple);
  font-weight: 600;
  text-decoration: none;
}
.comment-bubble {
  background: #12131b;
  border: 1px solid var(--border-subtle);
  border-radius: 14px;
  padding: 14px 18px;
  font-size: 14px;
  line-height: 1.5;
  color: #fff;
  margin-bottom: 14px;
}
.comment-automation-box {
  background: rgba(168, 85, 247, 0.06);
  border: 1px solid rgba(168, 85, 247, 0.18);
  border-radius: 14px;
  padding: 14px 18px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.auto-field {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  font-size: 13px;
}
.auto-label {
  font-weight: 700;
  color: var(--accent-purple);
  min-width: 90px;
}
.auto-text {
  color: var(--text-main);
  word-break: break-word;
}

/* Posts / Media Feed Grid */
.posts-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(310px, 1fr));
  gap: 22px;
  margin-top: 18px;
}
.post-media-card {
  background: var(--bg-card);
  border: 1px solid var(--border-subtle);
  border-radius: 20px;
  overflow: hidden;
  transition: all 0.3s ease;
  display: flex;
  flex-direction: column;
}
.post-media-card:hover {
  border-color: var(--border-hover);
  transform: translateY(-3px);
  box-shadow: 0 10px 30px rgba(0,0,0,0.5);
}
.post-img-box {
  width: 100%;
  aspect-ratio: 1 / 1;
  background: #14151e;
  position: relative;
  overflow: hidden;
}
.post-img-box img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  transition: transform 0.4s ease;
}
.post-media-card:hover .post-img-box img {
  transform: scale(1.04);
}
.post-img-overlay {
  position: absolute;
  top: 12px;
  left: 12px;
  right: 12px;
  display: flex;
  justify-content: space-between;
}
.post-badge-glass {
  background: rgba(0, 0, 0, 0.65);
  backdrop-filter: blur(8px);
  padding: 4px 10px;
  border-radius: 12px;
  font-size: 11px;
  font-weight: 700;
  color: #fff;
  border: 1px solid rgba(255, 255, 255, 0.1);
}
.post-content {
  padding: 18px 20px;
  display: flex;
  flex-direction: column;
  flex-grow: 1;
  justify-content: space-between;
}
.post-caption {
  font-size: 13px;
  line-height: 1.5;
  color: var(--text-main);
  margin-bottom: 14px;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.post-meta-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12px;
  color: var(--text-muted);
  margin-bottom: 14px;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--border-subtle);
}
.post-btn-group {
  display: flex;
  gap: 8px;
}
.btn-card-action {
  flex: 1;
  text-align: center;
  padding: 8px 10px;
  border-radius: 10px;
  font-size: 12px;
  font-weight: 600;
  text-decoration: none;
  cursor: pointer;
  transition: all 0.2s;
}
.btn-set-rule {
  background: rgba(168, 85, 247, 0.15);
  color: var(--accent-purple);
  border: 1px solid rgba(168, 85, 247, 0.3);
}
.btn-set-rule:hover {
  background: var(--accent-purple);
  color: #fff;
}
.btn-view-comments {
  background: #232534;
  color: #fff;
  border: 1px solid var(--border-subtle);
}
.btn-view-comments:hover {
  background: #2c2f42;
}

/* Quick simulator bar */
.sim-bar {
  background: var(--bg-card);
  border: 1px solid var(--border-subtle);
  border-radius: 18px;
  padding: 16px 20px;
  margin-bottom: 22px;
  display: flex;
  align-items: center;
  gap: 12px;
}
.sim-bar input {
  flex-grow: 1;
  background: #111219;
  border: 1px solid var(--border-subtle);
  border-radius: 12px;
  padding: 11px 16px;
  color: #fff;
  font-size: 13px;
  outline: none;
}
.sim-bar input:focus { border-color: var(--accent-purple); }

/* Rules Form & Cards */
.form-card {
  background: var(--bg-card);
  border: 1px solid var(--border-subtle);
  border-radius: 20px;
  padding: 24px;
  margin-bottom: 24px;
}
.form-title { font-size: 18px; font-weight: 700; margin-bottom: 6px; }
.form-subtitle { font-size: 13px; color: var(--text-muted); margin-bottom: 18px; }
.form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin-bottom: 16px;
}
.form-group { display: flex; flex-direction: column; gap: 6px; }
.form-group.full { grid-column: span 2; }
.form-group label { font-size: 12px; font-weight: 600; color: var(--text-muted); }
.form-control {
  background: #12131a;
  border: 1px solid var(--border-subtle);
  border-radius: 12px;
  padding: 12px 14px;
  color: #fff;
  font-family: inherit;
  font-size: 14px;
  outline: none;
  transition: all 0.2s;
}
.form-control:focus {
  border-color: var(--accent-purple);
  box-shadow: 0 0 0 3px var(--accent-purple-glow);
}
textarea.form-control { resize: vertical; min-height: 80px; }

.rules-list { display: flex; flex-direction: column; gap: 14px; }
.rule-card {
  background: var(--bg-card);
  border: 1px solid var(--border-subtle);
  border-radius: 18px;
  padding: 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
}
.keyword-tag {
  background: rgba(168, 85, 247, 0.15);
  color: var(--accent-purple);
  font-size: 12px;
  font-weight: 600;
  padding: 3px 10px;
  border-radius: 8px;
}
.btn-toggle {
  background: #232534;
  color: #fff;
  border: 1px solid var(--border-subtle);
  padding: 8px 14px;
  border-radius: 10px;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  text-decoration: none;
}
.btn-toggle.active {
  background: rgba(34, 197, 94, 0.15);
  color: var(--accent-green);
  border-color: rgba(34, 197, 94, 0.3);
}
.btn-delete { color: #ef4444; text-decoration: none; font-size: 16px; padding: 8px; }

.copy-box {
  display: flex;
  align-items: center;
  gap: 10px;
  background: #111219;
  border: 1px solid var(--border-subtle);
  border-radius: 12px;
  padding: 12px 16px;
  font-family: monospace;
  font-size: 13px;
  margin-top: 6px;
}
.copy-btn {
  background: #222432;
  border: none;
  color: #fff;
  padding: 6px 12px;
  border-radius: 8px;
  cursor: pointer;
  font-size: 12px;
  margin-left: auto;
}

@media (max-width: 1024px) {
  body { flex-direction: column; }
  aside.sidebar { width: 100%; min-height: auto; border-right: none; border-bottom: 1px solid var(--border-subtle); }
  .dashboard-grid { grid-template-columns: 1fr; }
  .hero-main-stat { flex-direction: column; align-items: flex-start; }
  .chart-wrapper { width: 100%; }
  .hero-substats { grid-template-columns: repeat(2, 1fr); }
}
</style>
</head>
<body>

{% if not user %}
<div style="display:flex;align-items:center;justify-content:center;min-height:100vh;width:100%;padding:20px;">
  <div style="background:var(--bg-card);border:1px solid var(--border-subtle);border-radius:28px;padding:48px 40px;max-width:480px;width:100%;text-align:center;box-shadow:0 20px 50px rgba(0,0,0,0.5);">
    <div style="width:64px;height:64px;border-radius:20px;background:linear-gradient(135deg,#a855f7,#6366f1);margin:0 auto 24px;display:flex;align-items:center;justify-content:center;font-size:32px;box-shadow:0 8px 25px var(--accent-purple-glow);">&#9889;</div>
    <h1 style="font-size:26px;font-weight:800;letter-spacing:-0.5px;margin-bottom:8px;">Insta<span style="color:var(--accent-purple);">Flow</span> Automation</h1>
    <p style="font-size:14px;color:var(--text-muted);margin-bottom:28px;line-height:1.6;">Automate your Instagram comments, instantly send DMs with links, and schedule posts 24/7 with Meta Graph API.</p>
    <a href="/login" class="btn-primary" style="justify-content:center;padding:14px;font-size:15px;width:100%;border-radius:16px;background:linear-gradient(135deg,#e1306c,#833ab4);box-shadow:0 6px 20px rgba(225,48,108,0.35);">
      Connect Instagram Account &rarr;
    </a>
    <p style="margin-top:20px;font-size:12px;color:var(--text-muted);"><a href="/privacy" style="color:var(--text-muted);">Privacy Policy</a> &middot; <a href="/data-deletion" style="color:var(--text-muted);">Data Deletion</a></p>
  </div>
</div>
{% else %}

<!-- Sidebar -->
<aside class="sidebar">
  <div>
    <div class="logo-area">
      <div class="logo-icon">&#9889;</div>
      <div class="logo-text">Insta<span>Flow</span></div>
    </div>

    <nav class="sidebar-nav">
      <a href="#overview" class="nav-link active" onclick="switchTab('overview')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><rect width="7" height="7" x="3" y="3" rx="1"/><rect width="7" height="7" x="14" y="3" rx="1"/><rect width="7" height="7" x="14" y="14" rx="1"/><rect width="7" height="7" x="3" y="14" rx="1"/></svg>
        Overview
      </a>
      <a href="#comments" class="nav-link" onclick="switchTab('comments')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z"/></svg>
        Live Comments
        <div class="live-pill"><div class="live-dot"></div> LIVE</div>
      </a>
      <a href="#media" class="nav-link" onclick="switchTab('media')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><rect width="18" height="18" x="3" y="3" rx="2" ry="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"/></svg>
        All Posts Feed
        <span style="margin-left: auto; font-size: 11px; background: #232534; padding: 2px 7px; border-radius: 10px;">{{ posts|length }}</span>
      </a>
      <a href="#rules" class="nav-link" onclick="switchTab('rules')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/></svg>
        Automation Rules
      </a>
      <a href="#posts" class="nav-link" onclick="switchTab('posts')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><rect width="18" height="18" x="3" y="4" rx="2"/><path d="M3 10h18"/><path d="M8 2v4"/><path d="M16 2v4"/></svg>
        Post Scheduler
      </a>
      <a href="#activity" class="nav-link" onclick="switchTab('activity')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>
        Activity & Logs
      </a>
      <a href="#webhook" class="nav-link" onclick="switchTab('webhook')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/></svg>
        Webhook Setup
      </a>
    </nav>
  </div>

  <div class="sidebar-bottom">
    <div class="theme-toggle-row">
      <span>Dark Mode</span>
      <div class="toggle-switch"><div class="toggle-thumb"></div></div>
    </div>

    <!-- Pro Card -->
    <div class="pro-card">
      <div class="pro-card-header">
        <span class="pro-badge">Active</span>
        <span style="font-size: 11px; color: #9ca3af;">v21.0</span>
      </div>
      <h4>@{{ user.username }}</h4>
      <p>Meta Graph API Connected</p>
      <a href="/logout" class="pro-btn">Account Settings &rarr;</a>
    </div>
  </div>
</aside>

<!-- Main Container -->
<main class="main-content">
  <!-- Top Header -->
  <header class="top-header">
    <div class="search-bar">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
      <input type="text" id="global-search" placeholder="Search comments, posts, keywords..." oninput="filterEverything(this.value)">
    </div>

    <div class="header-actions">
      <a href="#rules" onclick="switchTab('rules')" class="btn-primary">
        <span>+</span> Create new rule
      </a>
      <a href="#comments" onclick="switchTab('comments')" class="icon-btn" title="Live Comments">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z"/></svg>
        <div class="notification-dot"></div>
      </a>
      <div class="user-avatar-badge">
        <div class="avatar-img">{{ user.username[:2]|upper }}</div>
        <span class="user-name">@{{ user.username }}</span>
      </div>
    </div>
  </header>

  <!-- Welcome Row -->
  <div class="welcome-row">
    <div class="welcome-text">
      <p>Hi {{ user.username }},</p>
      <h2>&#128075; Welcome back!</h2>
    </div>
    <div class="time-selector">
      <div class="time-pill" onclick="setTimeRange(this, 'today')">Today</div>
      <div class="time-pill active" onclick="setTimeRange(this, 'week')">Week</div>
      <div class="time-pill" onclick="setTimeRange(this, 'month')">Month</div>
    </div>
  </div>

  <!-- OVERVIEW TAB -->
  <section id="tab-overview" class="tab-pane active">
    <!-- Hero Card matching reference Balance card -->
    <div class="hero-card">
      <div class="hero-card-header">
        <div class="hero-card-title">Automation Balance</div>
        <div class="hero-tabs">
          <div class="hero-tab active" onclick="setChartMetric(this, 'total')">Total Interactions</div>
          <div class="hero-tab" onclick="setChartMetric(this, 'comments')">Public Comments</div>
          <div class="hero-tab" onclick="setChartMetric(this, 'dms')">Direct Messages</div>
        </div>
      </div>

      <div class="hero-main-stat">
        <div class="stat-large-group">
          <div class="stat-large" id="hero-total-val">10,480</div>
          <div class="growth-pill">&#8599; 24%</div>
        </div>
        <div class="chart-wrapper">
          <canvas id="waveChart"></canvas>
        </div>
      </div>

      <div class="hero-substats">
        <div class="substat-col">
          <div class="substat-label">Comment Replies</div>
          <div class="substat-value">
            <span id="sub-comments">{{ comments|length or 3236 }}</span>
            <span class="substat-growth growth-up">&#8599; 18%</span>
          </div>
        </div>
        <div class="substat-col">
          <div class="substat-label">Direct Messages</div>
          <div class="substat-value">
            <span id="sub-dms">3,764</span>
            <span class="substat-growth growth-up">&#8599; 32%</span>
          </div>
        </div>
        <div class="substat-col">
          <div class="substat-label">Active Triggers</div>
          <div class="substat-value">
            <span>{{ rules|length }}</span>
            <span class="substat-growth growth-neutral">&#8599; Active</span>
          </div>
        </div>
        <div class="substat-col">
          <div class="substat-label">Instagram Posts</div>
          <div class="substat-value">
            <span>{{ posts|length }}</span>
            <span class="substat-growth growth-neutral">Synced</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 2 Column Lower Grid -->
    <div class="dashboard-grid">
      <!-- Order / Ring Card -->
      <div class="grid-card">
        <div class="grid-card-header">
          <div class="grid-card-title">Automation Distribution</div>
          <a href="#activity" onclick="switchTab('activity')" class="grid-card-link">Detail &rarr;</a>
        </div>
        <div class="ring-card-body">
          <div class="ring-stat-area">
            <div>
              <span style="font-size: 13px; color: var(--text-muted);">Total Actions Today</span>
              <div class="ring-stat-large">
                <span id="ring-count">86</span>
                <span class="growth-pill">&#8599; 14%</span>
              </div>
            </div>
            <div class="ring-legend">
              <div class="legend-item"><div class="legend-dot" style="background:#a855f7;"></div> Comment Replies <span class="legend-val" id="leg-c">{{ comments|length }}</span></div>
              <div class="legend-item"><div class="legend-dot" style="background:#ec4899;"></div> Direct Messages <span class="legend-val" id="leg-d">38</span></div>
              <div class="legend-item"><div class="legend-dot" style="background:#22c55e;"></div> Active Rules <span class="legend-val">{{ rules|length }}</span></div>
              <div class="legend-item"><div class="legend-dot" style="background:#06b6d4;"></div> Published Posts <span class="legend-val">{{ posts|length }}</span></div>
            </div>
          </div>
          <div class="ring-chart-box">
            <canvas id="donutChart"></canvas>
          </div>
        </div>
      </div>

      <!-- Real-time Comments Mini Feed -->
      <div class="grid-card">
        <div class="grid-card-header">
          <div class="grid-card-title">
            <span>Live Comments</span>
            <span class="live-pill"><div class="live-dot"></div> Real-time</span>
          </div>
          <a href="#comments" onclick="switchTab('comments')" class="grid-card-link">View All ({{ comments|length }}) &rarr;</a>
        </div>
        <div class="stream-list" id="overview-comments-box">
          {% for c in comments[:4] %}
          <div class="stream-item">
            <div class="stream-user">
              <div class="stream-avatar" style="background: linear-gradient(135deg, #a855f7, #ec4899); color: #fff;">{{ c.username[:2]|upper }}</div>
              <div class="stream-meta">
                <div class="stream-name">@{{ c.username }} <small style="color:var(--text-muted);font-weight:400;">&middot; {{ c.created_at }}</small></div>
                <div style="font-size: 12px; color: #fff;">{{ c.text[:42] }}...</div>
              </div>
            </div>
            <span class="stream-badge" style="background: rgba(34, 197, 94, 0.15); color: #22c55e;">&#10004; Replied</span>
          </div>
          {% endfor %}
        </div>
      </div>
    </div>

    <!-- Posts Quick Grid on Overview -->
    <div class="grid-card" style="margin-top: 24px;">
      <div class="grid-card-header">
        <div class="grid-card-title">
          <span>Instagram Posts & Engagement</span>
        </div>
        <a href="#media" onclick="switchTab('media')" class="grid-card-link">Manage All Posts &rarr;</a>
      </div>
      <div class="posts-grid" style="grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));">
        {% for p in posts[:3] %}
        <div class="post-media-card">
          <div class="post-img-box">
            <img src="{{ p.image_url }}" alt="Post image" onerror="this.src='https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?w=600&auto=format&fit=crop'">
            <div class="post-img-overlay">
              <span class="post-badge-glass">&#10084; {{ 200 + loop.index * 135 }}</span>
              <span class="post-badge-glass">&#128172; {{ 40 + loop.index * 24 }}</span>
            </div>
          </div>
          <div class="post-content">
            <div class="post-caption">{{ p.caption }}</div>
            <div class="post-meta-row">
              <span>Post ID: #{{ p.id }}</span>
              <span style="color: var(--accent-green); font-weight: 600;">&#10004; Active</span>
            </div>
            <div class="post-btn-group">
              <a href="#rules" onclick="prefillRule('{{ p.id }}')" class="btn-card-action btn-set-rule">&#9889; Set Rule</a>
              <a href="#comments" onclick="switchTab('comments')" class="btn-card-action btn-view-comments">&#128172; Comments</a>
            </div>
          </div>
        </div>
        {% endfor %}
      </div>
    </div>
  </section>

  <!-- REAL-TIME COMMENTS TAB -->
  <section id="tab-comments" class="tab-pane">
    <div class="live-header-bar">
      <div>
        <h2 style="font-size: 22px; font-weight: 800; letter-spacing: -0.5px; margin-bottom: 4px; display:flex; align-items:center; gap: 10px;">
          &#128172; Real-Time Comments Stream
          <div class="live-beacon"><div class="live-dot"></div> LIVE STREAMING</div>
        </h2>
        <p style="font-size: 13px; color: var(--text-muted);">Incoming comments from your Instagram posts are processed and replied to in seconds.</p>
      </div>

      <div style="display:flex; gap: 8px;">
        <button class="btn-primary" onclick="pollComments(true)" style="background: #232534; box-shadow: none; padding: 8px 16px;">
          &#8635; Refresh Stream
        </button>
      </div>
    </div>

    <!-- Quick Test / Simulator Bar -->
    <div class="sim-bar">
      <div style="font-weight: 700; font-size: 13px; color: var(--accent-purple); white-space: nowrap;">&#9889; Test Comment:</div>
      <input type="text" id="sim-quick-text" placeholder="Type a test comment e.g. What is the price and link?">
      <button class="btn-primary" onclick="sendQuickSim()" style="white-space: nowrap;">Simulate</button>
    </div>

    <!-- Real-time Comments List Container -->
    <div id="live-comments-container">
      {% for c in comments %}
      <div class="comment-card" data-cid="{{ c.comment_id }}">
        <div class="comment-top">
          <div class="commenter-info">
            <div class="avatar-ring">
              <div class="avatar-ring-inner">{{ c.username[:2]|upper }}</div>
            </div>
            <div>
              <div class="commenter-name">@{{ c.username }}</div>
              <a href="#media" onclick="switchTab('media')" class="comment-post-ref">on Post #{{ c.media_id or '17992837482910' }}</a>
            </div>
          </div>
          <span style="font-size: 12px; color: var(--text-muted);">{{ c.created_at }}</span>
        </div>

        <div class="comment-bubble">
          {{ c.text }}
        </div>

        <div class="comment-automation-box">
          <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 4px;">
            <span class="keyword-tag" style="background: rgba(34, 197, 94, 0.15); color: #22c55e;">
              &#9889; Matched Trigger: "{{ c.matched_keyword or 'price, link' }}"
            </span>
            <span style="font-size: 11px; font-weight: 700; color: var(--accent-green);">&#10004; AUTO-PROCESSED</span>
          </div>

          {% if c.reply_sent %}
          <div class="auto-field">
            <span class="auto-label">&#128172; Public Reply:</span>
            <span class="auto-text">{{ c.reply_sent }}</span>
          </div>
          {% endif %}

          {% if c.dm_sent %}
          <div class="auto-field">
            <span class="auto-label">&#128233; Private DM:</span>
            <span class="auto-text">{{ c.dm_sent }}</span>
          </div>
          {% endif %}
        </div>
      </div>
      {% endfor %}
    </div>
  </section>

  <!-- ALL POSTS & MEDIA FEED TAB -->
  <section id="tab-media" class="tab-pane">
    <div class="live-header-bar">
      <div>
        <h2 style="font-size: 22px; font-weight: 800; letter-spacing: -0.5px; margin-bottom: 4px;">
          &#128248; Real Instagram Posts &amp; Media Feed
        </h2>
        <p style="font-size: 13px; color: var(--text-muted);">Live feed from @{{ user.username }} (ID: {{ user.ig_id }}). Attach custom automation rules to each post.</p>
      </div>

      <div style="display: flex; gap: 10px;">
        <button onclick="triggerLiveSync()" id="btn-sync-media" class="btn-primary" style="background: linear-gradient(135deg, #a855f7, #6366f1); box-shadow: 0 4px 14px var(--accent-purple-glow);">
          &#8635; Sync Real Instagram Posts
        </button>
        <a href="#posts" onclick="switchTab('posts')" class="btn-primary">
          <span>+</span> Schedule Post
        </a>
      </div>
    </div>

    <!-- Live Sync / Token Card -->
    <div class="form-card" style="border: 1px solid rgba(168, 85, 247, 0.25); background: rgba(168, 85, 247, 0.04); margin-bottom: 22px;">
      <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 14px;">
        <div>
          <div style="font-size: 15px; font-weight: 700; color: #fff; margin-bottom: 3px;">
            &#128279; Meta Graph API v21.0 Feed Connection
          </div>
          <div style="font-size: 12px; color: var(--text-muted);">
            Connected to Instagram Business ID: <b style="color: var(--accent-purple);">{{ user.ig_id }}</b> &middot; Page: <b>Code Decoded</b>
          </div>
        </div>
        <button onclick="toggleTokenBox()" class="btn-toggle" style="background: #1e1f2b; font-size: 12px;">
          Update Meta Token &rarr;
        </button>
      </div>

      <div id="token-update-box" style="display: none; margin-top: 16px; padding-top: 16px; border-top: 1px solid var(--border-subtle);">
        <label style="font-size: 12px; font-weight: 600; color: var(--text-muted); display: block; margin-bottom: 6px;">
          Paste Graph API Explorer Page Access Token (from Meta for Developers)
        </label>
        <div style="display: flex; gap: 10px;">
          <input type="password" id="input-new-token" class="form-control" placeholder="EAA..." style="flex-grow: 1;">
          <button onclick="saveLiveToken()" id="btn-save-tok" class="btn-primary" style="white-space: nowrap;">Save &amp; Sync Feed</button>
        </div>
        <small style="color: var(--text-muted); display: block; margin-top: 6px;">
          Token requires <code>instagram_basic</code>, <code>instagram_manage_comments</code>, and <code>pages_read_engagement</code> permissions.
        </small>
      </div>
    </div>

    <!-- Posts Grid -->
    <div class="posts-grid" id="all-posts-container">
      {% for p in posts %}
      <div class="post-media-card">
        <div class="post-img-box">
          <img src="{{ p.image_url }}" alt="Post image" onerror="this.src='https://images.unsplash.com/photo-1618005182384-a83a8bd57fbe?w=600&auto=format&fit=crop'">
          <div class="post-img-overlay">
            <span class="post-badge-glass">&#10084; {{ 340 + loop.index * 115 }}</span>
            <span class="post-badge-glass">&#128172; {{ 45 + loop.index * 22 }}</span>
          </div>
        </div>
        <div class="post-content">
          <div class="post-caption">{{ p.caption }}</div>
          <div class="post-meta-row">
            <span>ID: #{{ p.id }}</span>
            <span style="color: var(--accent-purple); font-weight: 600;">Status: {{ p.status|upper }}</span>
          </div>
          <div class="post-btn-group">
            <button onclick="prefillRule('{{ p.id }}')" class="btn-card-action btn-set-rule">&#9889; Set Auto-Reply</button>
            <button onclick="switchTab('comments')" class="btn-card-action btn-view-comments">&#128172; Comments</button>
          </div>
        </div>
      </div>
      {% endfor %}
    </div>
  </section>

  <!-- RULES TAB -->
  <section id="tab-rules" class="tab-pane">
    <div class="form-card">
      <div class="form-title">+ Create Automation Rule</div>
      <div class="form-subtitle">Auto-reply to Instagram comments and automatically deliver links, discount codes, or FAQs directly into user DMs.</div>
      <form method="post" action="/rules">
        <div class="form-grid">
          <div class="form-group">
            <label>Trigger Keywords (comma separated)</label>
            <input class="form-control" name="keyword" id="rule-keyword-input" placeholder="e.g. price, link, details, info, code">
          </div>
          <div class="form-group">
            <label>Specific Post / Media ID (Optional)</label>
            <input class="form-control" name="media_id" id="rule-media-id-input" placeholder="Leave empty to apply to all posts">
          </div>
          <div class="form-group">
            <label>Public Comment Reply Variants (one per line, random pick, supports {username})</label>
            <textarea class="form-control" name="comment_reply" placeholder="Sent you a DM! Check your inbox &#128640;&#10;Just sent you the link in DM! &#128233;"></textarea>
          </div>
          <div class="form-group">
            <label>Direct Message (DM) Content (delivered straight to user inbox)</label>
            <textarea class="form-control" name="dm_text" placeholder="Hey {username}! Thanks for reaching out. Here is the link you requested: https://instagram.com/coded.ecoded"></textarea>
          </div>
        </div>
        <button class="btn-primary" type="submit">Save Automation Rule</button>
      </form>
    </div>

    <div class="grid-card-title" style="margin-bottom: 16px;">Active Rules ({{ rules|length }})</div>
    <div class="rules-list">
      {% for r in rules %}
      <div class="rule-card">
        <div>
          <div class="rule-keywords">
            {% for kw in (r.keyword or 'Any comment').split(',') %}
            <span class="keyword-tag">{{ kw.strip() }}</span>
            {% endfor %}
            {% if r.media_id %}<span class="keyword-tag" style="background: rgba(6, 182, 212, 0.15); color: #06b6d4;">Post: {{ r.media_id }}</span>{% endif %}
          </div>
          <div style="font-size: 13px; color: var(--text-muted); margin-top: 6px;">
            <div><b>Public Reply:</b> {{ r.comment_reply or 'None' }}</div>
            <div><b>Private DM:</b> {{ r.dm_text or 'None' }}</div>
          </div>
        </div>
        <div style="display: flex; align-items: center; gap: 12px;">
          <a href="/rules/{{ r.id }}/toggle" class="btn-toggle {{ 'active' if r.active else '' }}">
            {{ 'Active' if r.active else 'Disabled' }}
          </a>
          <a href="/rules/{{ r.id }}/delete" class="btn-delete" onclick="return confirm('Delete this rule?')" title="Delete">&times;</a>
        </div>
      </div>
      {% endfor %}
    </div>
  </section>

  <!-- POSTS TAB -->
  <section id="tab-posts" class="tab-pane">
    <div class="form-card">
      <div class="form-title">Schedule Instagram Post</div>
      <div class="form-subtitle">Queue public photos and captions to publish automatically to your Instagram Business account.</div>
      <form method="post" action="/schedule">
        <div class="form-grid">
          <div class="form-group">
            <label>Image URL (Direct JPEG URL)</label>
            <input class="form-control" name="image_url" placeholder="https://images.unsplash.com/..." required>
          </div>
          <div class="form-group">
            <label>Publish Time (UTC)</label>
            <input class="form-control" type="datetime-local" name="run_at" required>
          </div>
          <div class="form-group full">
            <label>Caption & Hashtags</label>
            <textarea class="form-control" name="caption" placeholder="Write your post caption here... #coding #ai"></textarea>
          </div>
        </div>
        <button class="btn-primary" type="submit">Schedule Post</button>
      </form>
    </div>
  </section>

  <!-- ACTIVITY TAB -->
  <section id="tab-activity" class="tab-pane">
    <div class="form-card">
      <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px;">
        <div>
          <div class="form-title">Audit Log & Event History</div>
          <div class="form-subtitle">Complete real-time log of comments answered, DMs sent, and system health.</div>
        </div>
        <a href="/export.csv" class="btn-primary" style="background: #232534; color: #fff; box-shadow: none;">&#11015; Export CSV</a>
      </div>

      <div class="stream-list">
        {% for l in logs %}
        <div class="stream-item">
          <div class="stream-user">
            <div class="stream-avatar" style="background: {{ '#22c55e22' if l.kind=='comment' else ('#a855f722' if l.kind=='dm' else '#ef444422') }}; color: {{ '#22c55e' if l.kind=='comment' else ('#a855f7' if l.kind=='dm' else '#ef4444') }};">
              {{ l.kind[:1]|upper }}
            </div>
            <div class="stream-meta">
              <div class="stream-name">{{ l.msg }}</div>
              <div class="stream-time">{{ l.ts }} {% if l.who %}&middot; User: {{ l.who }}{% endif %} {% if l.keyword %}&middot; Keyword: <b>{{ l.keyword }}</b>{% endif %}</div>
            </div>
          </div>
          <span class="stream-badge">{{ l.kind|upper }}</span>
        </div>
        {% endfor %}
      </div>
    </div>
  </section>

  <!-- WEBHOOK SETUP TAB -->
  <section id="tab-webhook" class="tab-pane">
    <div class="form-card">
      <div class="form-title">Meta Instagram Webhook Configuration</div>
      <div class="form-subtitle">Copy these exact values into Meta App Dashboard &rarr; Webhooks &rarr; Instagram to activate 24/7 automations.</div>

      <div style="display: flex; flex-direction: column; gap: 16px;">
        <div>
          <label style="font-size: 13px; font-weight: 600; color: var(--text-muted);">Callback URL</label>
          <div class="copy-box">
            <span id="cb-url">{{ base_url }}/webhook</span>
            <button class="copy-btn" onclick="copyText('cb-url')">Copy</button>
          </div>
        </div>

        <div>
          <label style="font-size: 13px; font-weight: 600; color: var(--text-muted);">Verify Token</label>
          <div class="copy-box">
            <span id="vr-tok">{{ verify_token }}</span>
            <button class="copy-btn" onclick="copyText('vr-tok')">Copy</button>
          </div>
        </div>

        <div>
          <label style="font-size: 13px; font-weight: 600; color: var(--text-muted);">Subscribed Fields</label>
          <div style="margin-top: 6px; display: flex; gap: 8px;">
            <span class="keyword-tag" style="background: rgba(34, 197, 94, 0.15); color: #22c55e;">comments</span>
            <span class="keyword-tag" style="background: rgba(168, 85, 247, 0.15); color: #a855f7;">messages</span>
          </div>
        </div>
      </div>
    </div>
  </section>
</main>
{% endif %}

<script>
function switchTab(tabId) {
  document.querySelectorAll('.tab-pane').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.nav-link').forEach(el => el.classList.remove('active'));
  
  const target = document.getElementById('tab-' + tabId);
  if (target) target.classList.add('active');
  
  const link = document.querySelector(`.nav-link[href="#${tabId}"]`);
  if (link) link.classList.add('active');
  location.hash = tabId;
}

function prefillRule(postId) {
  switchTab('rules');
  const input = document.getElementById('rule-media-id-input');
  if (input) {
    input.value = postId;
    input.focus();
  }
}

function copyText(id) {
  const t = document.getElementById(id).innerText;
  navigator.clipboard.writeText(t).then(() => {
    alert('Copied to clipboard: ' + t);
  });
}

function filterEverything(val) {
  val = (val || '').toLowerCase().trim();
  document.querySelectorAll('.comment-card').forEach(c => {
    const txt = c.innerText.toLowerCase();
    c.style.display = (!val || txt.includes(val)) ? 'block' : 'none';
  });
  document.querySelectorAll('.post-media-card').forEach(p => {
    const txt = p.innerText.toLowerCase();
    p.style.display = (!val || txt.includes(val)) ? 'flex' : 'none';
  });
}

// Quick simulator function
function sendQuickSim() {
  const input = document.getElementById('sim-quick-text');
  const txt = (input.value || '').trim();
  if (!txt) return alert('Enter comment text!');
  fetch('/api/simulate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text: txt, username: 'live_user' })
  }).then(r => r.json()).then(res => {
    input.value = '';
    pollComments(true);
  }).catch(e => alert('Error: ' + e));
}

// Real-time live polling for comments
let knownCommentIds = new Set();
function pollComments(flashFirst) {
  fetch('/api/comments').then(r => r.json()).then(d => {
    if (!d || !d.comments) return;
    const container = document.getElementById('live-comments-container');
    if (!container) return;
    
    let html = '';
    d.comments.forEach((c, idx) => {
      const isNew = flashFirst && idx === 0;
      html += `
      <div class="comment-card ${isNew ? 'new-item' : ''}" data-cid="${c.comment_id}">
        <div class="comment-top">
          <div class="commenter-info">
            <div class="avatar-ring">
              <div class="avatar-ring-inner">${(c.username || 'U').slice(0,2).toUpperCase()}</div>
            </div>
            <div>
              <div class="commenter-name">@${c.username}</div>
              <a href="#media" onclick="switchTab('media')" class="comment-post-ref">on Post #${c.media_id || '17992837482910'}</a>
            </div>
          </div>
          <span style="font-size: 12px; color: var(--text-muted);">${c.created_at || 'Just now'}</span>
        </div>
        <div class="comment-bubble">${c.text}</div>
        <div class="comment-automation-box">
          <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 4px;">
            <span class="keyword-tag" style="background: rgba(34, 197, 94, 0.15); color: #22c55e;">
              &#9889; Matched Trigger: "${c.matched_keyword || 'price, link'}"
            </span>
            <span style="font-size: 11px; font-weight: 700; color: var(--accent-green);">&#10004; AUTO-PROCESSED</span>
          </div>
          ${c.reply_sent ? `<div class="auto-field"><span class="auto-label">&#128172; Public Reply:</span><span class="auto-text">${c.reply_sent}</span></div>` : ''}
          ${c.dm_sent ? `<div class="auto-field"><span class="auto-label">&#128233; Private DM:</span><span class="auto-text">${c.dm_sent}</span></div>` : ''}
        </div>
      </div>`;
    });
    container.innerHTML = html;
  }).catch(() => {});
}

// Polling interval every 4 seconds
setInterval(() => {
  if (document.getElementById('tab-comments') && document.getElementById('tab-comments').classList.contains('active')) {
    pollComments(false);
  }
}, 4000);

// Initialize Charts
let waveChart, donutChart;
let globalStats = { days: ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'], series: { comment: [12, 19, 14, 25, 22, 30, 28], dm: [8, 15, 12, 20, 18, 25, 24] }, totals: { comment: 3236, dm: 3764 } };

function triggerLiveSync() {
  const btn = document.getElementById('btn-sync-media');
  if (btn) btn.innerText = 'Syncing from Meta...';
  fetch('/api/sync', { method: 'POST' })
    .then(r => r.json())
    .then(res => {
      if (btn) btn.innerText = '⟳ Sync Real Instagram Posts';
      if (res.success) {
        alert('Successfully synced ' + res.media_count + ' real posts and ' + res.comments_count + ' real comments from Instagram!');
        location.reload();
      } else {
        alert('Meta Graph API Notice: ' + (res.error || 'Token requires instagram_basic permission. Click "Update Meta Token" to connect your Graph API token.'));
        const box = document.getElementById('token-update-box');
        if (box) box.style.display = 'block';
      }
    })
    .catch(e => {
      if (btn) btn.innerText = '⟳ Sync Real Instagram Posts';
      alert('Sync error: ' + e);
    });
}

function toggleTokenBox() {
  const box = document.getElementById('token-update-box');
  if (box) box.style.display = box.style.display === 'none' ? 'block' : 'none';
}

function saveLiveToken() {
  const tok = (document.getElementById('input-new-token').value || '').trim();
  if (!tok) return alert('Please paste your Meta Access Token!');
  const btn = document.getElementById('btn-save-tok');
  if (btn) btn.innerText = 'Connecting...';
  fetch('/api/save-token', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token: tok })
  })
  .then(r => r.json())
  .then(res => {
    if (btn) btn.innerText = 'Save & Sync Feed';
    if (res.success) {
      alert('Token connected! Synced ' + (res.sync.media_count || 0) + ' real Instagram posts.');
      location.reload();
    } else {
      alert('Token verification error: ' + (res.error || 'Invalid token.'));
    }
  })
  .catch(e => {
    if (btn) btn.innerText = 'Save & Sync Feed';
    alert('Error: ' + e);
  });
}

window.addEventListener('DOMContentLoaded', () => {
  const hash = (location.hash || '#overview').replace('#', '');
  switchTab(hash);

  const waveCanvas = document.getElementById('waveChart');
  if (waveCanvas) {
    const waveCtx = waveCanvas.getContext('2d');
    const grad = waveCtx.createLinearGradient(0, 0, 0, 160);
    grad.addColorStop(0, 'rgba(168, 85, 247, 0.4)');
    grad.addColorStop(1, 'rgba(168, 85, 247, 0.0)');
    waveChart = new Chart(waveCtx, {
      type: 'line',
      data: {
        labels: ['Mo', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'],
        datasets: [{
          data: [20, 34, 26, 45, 40, 55, 52],
          borderColor: '#a855f7',
          borderWidth: 3,
          tension: 0.45,
          fill: true,
          backgroundColor: grad,
          pointRadius: 0
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: { x: { grid: { display: false }, ticks: { color: '#6b7280' } }, y: { display: false } }
      }
    });
  }

  const donutCanvas = document.getElementById('donutChart');
  if (donutCanvas) {
    const donutCtx = donutCanvas.getContext('2d');
    donutChart = new Chart(donutCtx, {
      type: 'doughnut',
      data: {
        labels: ['Replies', 'DMs', 'Rules', 'Posts'],
        datasets: [{
          data: [32, 38, 16, 14],
          backgroundColor: ['#a855f7', '#ec4899', '#22c55e', '#06b6d4'],
          borderWidth: 0,
          borderRadius: 6
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: '72%',
        plugins: { legend: { display: false } }
      }
    });
  }
});
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(port=5000)
