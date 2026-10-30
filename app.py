"""SnapTrack - multi-user receipt tracker with deadline alerts."""
import base64, hashlib, hmac, json, logging, os, re, secrets, smtplib, sqlite3, threading, time, uuid
from collections import defaultdict
from contextlib import asynccontextmanager, closing
from datetime import date
from email.message import EmailMessage
from pathlib import Path

import requests
from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

_env = Path(__file__).with_name(".env")
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

log = logging.getLogger("snaptrack")
logging.basicConfig(level=logging.INFO)

BASE = Path(__file__).parent
DATA = Path(os.getenv("SNAPTRACK_DATA", BASE / "data"))
UPLOADS = DATA / "uploads"
UPLOADS.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA / "snaptrack.db"
MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
ALERT_DAYS = sorted({int(x) for x in os.getenv("ALERT_DAYS", "7,3,1,0").split(",") if x.strip()})
ALERT_HOUR = int(os.getenv("ALERT_HOUR", "9"))
SIGNUP_CODE = os.getenv("SIGNUP_CODE", "")
DAILY_UPLOAD_LIMIT = int(os.getenv("DAILY_UPLOAD_LIMIT", "20"))
SESSION_DAYS = 30
MIME_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
            "application/pdf": ".pdf"}
KINDS = {"renewal", "warranty_end", "return_deadline", "due_date", "trial_end", "other"}
MAX_BYTES = 10 * 1024 * 1024
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
lock = threading.Lock()

# ---------- database ----------
def db():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c

def init_db():
    with closing(db()) as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY AUTOINCREMENT, identifier TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
          pw TEXT NOT NULL, alert_email TEXT DEFAULT '', ntfy_topic TEXT DEFAULT '',
          created_at TEXT DEFAULT (datetime('now')));
        CREATE TABLE IF NOT EXISTS sessions(
          token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS receipts(
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          filename TEXT, thumb TEXT, mime TEXT, store TEXT, total REAL, tax REAL, currency TEXT, category TEXT,
          doc_type TEXT, document_number TEXT, purchase_date TEXT, items TEXT DEFAULT '[]', notes TEXT,
          created_at TEXT DEFAULT (datetime('now')));
        CREATE INDEX IF NOT EXISTS idx_receipts_user ON receipts(user_id);
        CREATE TABLE IF NOT EXISTS deadlines(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          receipt_id INTEGER NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
          kind TEXT, label TEXT, date TEXT NOT NULL, alerted TEXT DEFAULT '[]');
        """)
        c.commit()

def backup_db():
    dest = DATA / "backup"
    dest.mkdir(exist_ok=True)
    with closing(db()) as src, closing(sqlite3.connect(dest / "snaptrack-backup.db")) as dst:
        src.backup(dst)

# ---------- helpers ----------
def valid_date(s):
    try:
        return date.fromisoformat(str(s)).isoformat()
    except (ValueError, TypeError):
        return None

def clean_str(v, n=200):
    return str(v).strip()[:n] if v not in (None, "") else None

def clean_num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def sha(s): return hashlib.sha256(s.encode()).hexdigest()

# ---------- passwords / sessions ----------
def hash_pw(pw):
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${h.hex()}"

def check_pw(pw, stored):
    try:
        _, s, h = stored.split("$")
        calc = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(s), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(calc.hex(), h)
    except ValueError:
        return False

DUMMY_HASH = hash_pw("dummy-password")
_attempts = defaultdict(list)

def throttle(key, limit, window):
    now = time.time()
    _attempts[key] = [t for t in _attempts[key] if now - t < window]
    if len(_attempts[key]) >= limit:
        raise HTTPException(429, "Too many attempts, please try again later")

def record(key): _attempts[key].append(time.time())

def normalize(ident):
    ident = (ident or "").strip()
    if "@" in ident:
        ident = ident.lower()
        if not EMAIL_RE.match(ident) or len(ident) > 254:
            raise HTTPException(400, "Enter a valid email address")
        return ident, "email"
    digits = re.sub(r"[\s\-().]", "", ident)
    if not re.fullmatch(r"\+?\d{8,15}", digits):
        raise HTTPException(400, "Enter a valid email or phone number (digits, optional +country code)")
    return digits.lstrip("+"), "phone"

def start_session(c, uid, request, response):
    tok = secrets.token_urlsafe(32)
    now = int(time.time())
    c.execute("DELETE FROM sessions WHERE expires < ?", (now,))
    c.execute("INSERT INTO sessions(token,user_id,expires) VALUES(?,?,?)", (sha(tok), uid, now + SESSION_DAYS * 86400))
    c.commit()
    response.set_cookie("st_session", tok, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax",
                        secure=request.url.scheme == "https", path="/")

def current_user(request: Request):
    tok = request.cookies.get("st_session")
    if tok:
        with closing(db()) as c:
            row = c.execute("""SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id
                               WHERE s.token=? AND s.expires>?""", (sha(tok), int(time.time()))).fetchone()
        if row:
            return dict(row)
    raise HTTPException(401, "Please log in")

# ---------- AI extraction (Gemini) ----------
PROMPT = """You read photos/scans of receipts, bills, invoices, subscription confirmations and warranty cards.
Today's date is {today}. Return ONLY a JSON object (no prose, no code fences):
{"store": string|null, "total": number|null, "tax": number|null, "currency": "ISO 4217 code"|null,
 "category": one of ["groceries","dining","shopping","electronics","utilities","subscription","travel","health","home","other"],
 "doc_type": one of ["receipt","bill","invoice","subscription","warranty","other"],
 "document_number": string|null (receipt/invoice/order number),
 "purchase_date": "YYYY-MM-DD"|null,
 "items": [{"name": string, "qty": number|null, "price": number|null}]  (max 25 main line items),
 "deadlines": [{"kind": one of ["renewal","warranty_end","return_deadline","due_date","trial_end"], "label": short string, "date": "YYYY-MM-DD"}],
 "notes": short string}
Rules: total is the final amount charged/due. Only include deadlines that the document states or that follow directly
from stated terms (e.g. "30-day returns" + purchase date => return_deadline; "1 year warranty" => warranty_end;
"renews on ..." => renewal; free trial end => trial_end; bill due date => due_date). Never invent terms that are not
on the document. If a date has no year, use the nearest future occurrence. Use null / [] when unknown."""

def extract(data: bytes, media_type: str) -> dict:
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise HTTPException(500, "GEMINI_API_KEY is not set on the server")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    payload = {
        "contents": [{"parts": [
            {"text": PROMPT.replace("{today}", date.today().isoformat())},
            {"inline_data": {"mime_type": media_type, "data": base64.standard_b64encode(data).decode()}},
        ]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0},
    }
    r = None
    for _ in range(2):
        r = requests.post(url, headers={"x-goog-api-key": key}, json=payload, timeout=90)
        if r.status_code != 429:
            break
        time.sleep(8)
    if r.status_code == 429:
        raise HTTPException(429, "AI free-tier limit reached, try again in a minute")
    if r.status_code != 200:
        try:
            detail = r.json()["error"]["message"]
        except Exception:  # noqa
            detail = r.text[:200]
        raise HTTPException(502, f"AI error {r.status_code}: {detail}")
    try:
        parts = r.json()["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts)
        return json.loads(text[text.index("{"): text.rindex("}") + 1])
    except (KeyError, IndexError, ValueError):
        raise HTTPException(502, "Could not understand the AI response, please retry")

# ---------- notifications ----------
def send_email(to, subject, body):
    host = os.getenv("SMTP_HOST")
    if not host:
        raise RuntimeError("server email (SMTP) is not configured")
    msg = EmailMessage()
    msg["From"] = os.getenv("SMTP_FROM") or os.getenv("SMTP_USER", "snaptrack@localhost")
    msg["To"], msg["Subject"] = to, subject
    msg.set_content(body)
    port = int(os.getenv("SMTP_PORT", "587"))
    if port == 465:
        s = smtplib.SMTP_SSL(host, port, timeout=20)
    else:
        s = smtplib.SMTP(host, port, timeout=20)
        s.starttls()
    with s:
        if os.getenv("SMTP_USER"):
            s.login(os.getenv("SMTP_USER"), os.getenv("SMTP_PASS", ""))
        s.send_message(msg)

def send_ntfy(topic, title, body):
    r = requests.post(f"https://ntfy.sh/{topic}", data=body.encode(), timeout=15,
                      headers={"Title": title, "Priority": "high", "Tags": "bell"})
    r.raise_for_status()

def notify(email, topic, subject, body, short):
    sent, errors = False, []
    for name, wanted, fn in (("email", email, lambda: send_email(email, subject, body)),
                             ("phone push", topic, lambda: send_ntfy(topic, subject, short))):
        if not wanted:
            continue
        try:
            fn()
            sent = True
        except Exception as e:  # noqa
            log.warning("%s alert failed: %s", name, e)
            errors.append(f"{name}: {e}")
    if not sent and not errors:
        errors.append("No alert channel set. Add an email or phone-push topic in Settings.")
    return sent, errors

LABELS = {"renewal": "Renews", "warranty_end": "Warranty ends", "return_deadline": "Return window closes",
          "due_date": "Payment due", "trial_end": "Free trial ends", "other": "Deadline"}

def check_alerts():
    with lock, closing(db()) as c:
        rows = c.execute("""SELECT d.id did, d.kind, d.label, d.date, d.alerted, r.store, r.total, r.currency,
                                   r.user_id, u.alert_email, u.ntfy_topic
                            FROM deadlines d JOIN receipts r ON r.id=d.receipt_id JOIN users u ON u.id=r.user_id
                            WHERE d.date >= ?""", (date.today().isoformat(),)).fetchall()
        per_user = defaultdict(list)
        for r in rows:
            left = (date.fromisoformat(r["date"]) - date.today()).days
            hit = min((t for t in ALERT_DAYS if t >= left), default=None)
            if hit is not None and hit not in set(json.loads(r["alerted"])):
                per_user[r["user_id"]].append((r, left))
        count = 0
        def when(n): return "TODAY" if n == 0 else "tomorrow" if n == 1 else f"in {n} days"
        for due in per_user.values():
            due.sort(key=lambda x: x[1])
            lines, short = [], []
            for r, left in due:
                amt = f" ({r['total']:.2f} {r['currency'] or ''})" if r["total"] is not None else ""
                lines.append(f"- {LABELS.get(r['kind'], 'Deadline')}: {r['store'] or 'Unknown'}{amt} - "
                             f"{r['date']} ({when(left)})" + (f"\n  {r['label']}" if r["label"] else ""))
                short.append(f"{r['store'] or 'Item'}: {LABELS.get(r['kind'], 'deadline').lower()} {when(left)}")
            first = due[0][0]
            subject = f"SnapTrack: {len(due)} deadline(s) coming up"
            body = "Heads up! These need your attention:\n\n" + "\n".join(lines) + "\n\nCancel or return in time - SnapTrack"
            ok, errs = notify(first["alert_email"], first["ntfy_topic"], subject, body, "; ".join(short))
            if ok:
                for r, left in due:
                    sent = set(json.loads(r["alerted"])) | {t for t in ALERT_DAYS if t >= left}
                    c.execute("UPDATE deadlines SET alerted=? WHERE id=?", (json.dumps(sorted(sent)), r["did"]))
                c.commit()
                count += len(due)
            else:
                log.warning("could not alert user %s: %s", first["user_id"], errs)
        return count

# ---------- app ----------
@asynccontextmanager
async def lifespan(app):
    init_db()
    from apscheduler.schedulers.background import BackgroundScheduler
    sch = BackgroundScheduler()
    sch.add_job(check_alerts, "cron", hour=ALERT_HOUR, minute=0)
    sch.add_job(backup_db, "cron", hour=3, minute=0)
    sch.add_job(check_alerts, "date")
    sch.start()
    yield
    sch.shutdown(wait=False)

app = FastAPI(title="SnapTrack", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' blob: data:; "
                                               "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
    return resp

# ----- auth routes -----
class AuthIn(BaseModel):
    identifier: str
    password: str
    code: str = ""

@app.get("/api/config")
def config():
    return {"signup_code_required": bool(SIGNUP_CODE)}

@app.post("/api/register")
def register(body: AuthIn, request: Request, response: Response):
    ip = request.client.host if request.client else "?"
    throttle(f"reg:{ip}", 10, 3600)
    record(f"reg:{ip}")
    ident, kind = normalize(body.identifier)
    if not 8 <= len(body.password) <= 200:
        raise HTTPException(400, "Password must be at least 8 characters")
    with closing(db()) as c:
        try:
            cur = c.execute("INSERT INTO users(identifier,kind,pw,alert_email) VALUES(?,?,?,?)",
                            (ident, kind, hash_pw(body.password), ident if kind == "email" else ""))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "That email/number is already registered")
        c.commit()
        start_session(c, cur.lastrowid, request, response)
    return {"ok": True}

@app.post("/api/login")
def login(body: AuthIn, request: Request, response: Response):
    ident, _ = normalize(body.identifier)
    ip = request.client.host if request.client else "?"
    k1, k2 = f"login:{ident}:{ip}", f"login:{ident}"
    throttle(k1, 8, 600)
    throttle(k2, 30, 3600)
    with closing(db()) as c:
        u = c.execute("SELECT * FROM users WHERE identifier=?", (ident,)).fetchone()
        ok = check_pw(body.password, u["pw"] if u else DUMMY_HASH)
        if not (u and ok):
            record(k1), record(k2)
            raise HTTPException(401, "Wrong email/number or password")
        start_session(c, u["id"], request, response)
    return {"ok": True}

@app.post("/api/logout")
def logout(request: Request, response: Response):
    tok = request.cookies.get("st_session")
    if tok:
        with closing(db()) as c:
            c.execute("DELETE FROM sessions WHERE token=?", (sha(tok),))
            c.commit()
    response.delete_cookie("st_session", path="/")
    return {"ok": True}

@app.get("/api/me")
def me(user=Depends(current_user)):
    return {"identifier": user["identifier"], "kind": user["kind"], "alert_email": user["alert_email"],
            "ntfy_topic": user["ntfy_topic"], "smtp": bool(os.getenv("SMTP_HOST")),
            "ai": bool(os.getenv("GEMINI_API_KEY")), "alert_days": ALERT_DAYS}

class MeIn(BaseModel):
    alert_email: str = ""
    ntfy_topic: str = ""

@app.put("/api/me")
def update_me(body: MeIn, user=Depends(current_user)):
    email, topic = body.alert_email.strip().lower(), body.ntfy_topic.strip()
    if email and not EMAIL_RE.match(email):
        raise HTTPException(400, "Alert email is not valid")
    if topic and not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", topic):
        raise HTTPException(400, "Topic: 6-64 letters, digits, - or _")
    with closing(db()) as c:
        c.execute("UPDATE users SET alert_email=?, ntfy_topic=? WHERE id=?", (email, topic, user["id"]))
        c.commit()
    return {"ok": True}

@app.post("/api/test-alert")
def test_alert(user=Depends(current_user)):
    ok, errs = notify(user["alert_email"], user["ntfy_topic"], "SnapTrack test alert",
                      "It works! You will get alerts like this before your deadlines.", "SnapTrack test: it works!")
    if not ok:
        raise HTTPException(400, "; ".join(errs))
    return {"ok": True}

# ----- receipts -----
def own(c, rid, uid):
    r = c.execute("SELECT * FROM receipts WHERE id=? AND user_id=?", (rid, uid)).fetchone()
    if not r:
        raise HTTPException(404, "Not found")
    return r

def receipt_dict(c, r):
    d = {k: r[k] for k in ("id", "store", "total", "tax", "currency", "category", "doc_type", "document_number",
                           "purchase_date", "notes", "mime", "created_at")}
    d["items"] = json.loads(r["items"] or "[]")
    d["has_thumb"] = bool(r["thumb"])
    d["deadlines"] = [dict(x) for x in c.execute(
        "SELECT id, kind, label, date FROM deadlines WHERE receipt_id=? ORDER BY date", (r["id"],))]
    return d

@app.post("/api/upload")
def upload(file: UploadFile = File(...), thumb: UploadFile | None = File(None), user=Depends(current_user)):
    with closing(db()) as c:
        n = c.execute("SELECT COUNT(*) FROM receipts WHERE user_id=? AND date(created_at)=date('now')",
                      (user["id"],)).fetchone()[0]
    if n >= DAILY_UPLOAD_LIMIT:
        raise HTTPException(429, f"Daily upload limit ({DAILY_UPLOAD_LIMIT}) reached, try again tomorrow")
    data = file.file.read()
    if len(data) > MAX_BYTES:
        raise HTTPException(413, "File too large (max 10 MB)")
    mt = file.content_type if file.content_type in MIME_EXT else None
    if not mt:
        raise HTTPException(415, "Upload a JPG, PNG, WEBP, GIF or PDF")
    info = extract(data, mt)
    base = uuid.uuid4().hex
    name = base + MIME_EXT[mt]
    (UPLOADS / name).write_bytes(data)
    tname = None
    if thumb is not None and mt.startswith("image/"):
        t = thumb.file.read(400_001)
        if t[:2] == b"\xff\xd8" and len(t) <= 400_000:
            tname = base + "_t.jpg"
            (UPLOADS / tname).write_bytes(t)
    items = []
    for i in (info.get("items") or [])[:40]:
        if isinstance(i, dict) and i.get("name"):
            items.append({"name": clean_str(i["name"], 120), "qty": clean_num(i.get("qty")),
                          "price": clean_num(i.get("price"))})
    with closing(db()) as c:
        cur = c.execute("""INSERT INTO receipts(user_id,filename,thumb,mime,store,total,tax,currency,category,doc_type,
                           document_number,purchase_date,items,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (user["id"], name, tname, mt, clean_str(info.get("store")), clean_num(info.get("total")),
                         clean_num(info.get("tax")), clean_str(info.get("currency"), 8),
                         clean_str(info.get("category"), 30) or "other", clean_str(info.get("doc_type"), 30) or "receipt",
                         clean_str(info.get("document_number"), 80), valid_date(info.get("purchase_date")),
                         json.dumps(items), clean_str(info.get("notes"), 500)))
        rid = cur.lastrowid
        for d in info.get("deadlines") or []:
            dt = valid_date(d.get("date")) if isinstance(d, dict) else None
            if dt:
                k = d.get("kind") if d.get("kind") in KINDS else "other"
                c.execute("INSERT INTO deadlines(receipt_id,kind,label,date) VALUES(?,?,?,?)",
                          (rid, k, clean_str(d.get("label")) or "", dt))
        c.commit()
        result = receipt_dict(c, c.execute("SELECT * FROM receipts WHERE id=?", (rid,)).fetchone())
    threading.Thread(target=check_alerts, daemon=True).start()
    return result

@app.get("/api/receipts")
def list_receipts(user=Depends(current_user)):
    with closing(db()) as c:
        return [receipt_dict(c, r) for r in
                c.execute("SELECT * FROM receipts WHERE user_id=? ORDER BY id DESC", (user["id"],))]

@app.get("/api/receipts/{rid}")
def get_receipt(rid: int, user=Depends(current_user)):
    with closing(db()) as c:
        return receipt_dict(c, own(c, rid, user["id"]))

@app.get("/api/receipts/{rid}/file")
def get_file(rid: int, user=Depends(current_user)):
    with closing(db()) as c:
        r = own(c, rid, user["id"])
    p = UPLOADS / r["filename"]
    if not p.exists():
        raise HTTPException(404, "File missing")
    return FileResponse(p, media_type=r["mime"], headers={"Cache-Control": "private, max-age=3600"})

@app.get("/api/receipts/{rid}/thumb")
def get_thumb(rid: int, user=Depends(current_user)):
    with closing(db()) as c:
        r = own(c, rid, user["id"])
    if r["thumb"] and (UPLOADS / r["thumb"]).exists():
        return FileResponse(UPLOADS / r["thumb"], media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})
    if r["mime"].startswith("image/") and (UPLOADS / r["filename"]).exists():
        return FileResponse(UPLOADS / r["filename"], media_type=r["mime"], headers={"Cache-Control": "private, max-age=3600"})
    raise HTTPException(404)

class ReceiptEdit(BaseModel):
    store: str | None = None
    total: float | None = None
    currency: str | None = None
    category: str | None = None
    purchase_date: str | None = None
    notes: str | None = None

@app.put("/api/receipts/{rid}")
def edit_receipt(rid: int, body: ReceiptEdit, user=Depends(current_user)):
    f = body.model_dump(exclude_unset=True)
    if f.get("purchase_date") and not valid_date(f["purchase_date"]):
        raise HTTPException(400, "Date must be YYYY-MM-DD")
    for k, n in (("store", 200), ("currency", 8), ("category", 30), ("notes", 500)):
        if k in f and f[k] is not None:
            f[k] = f[k][:n]
    with closing(db()) as c:
        own(c, rid, user["id"])
        if f:
            c.execute(f"UPDATE receipts SET {', '.join(k + '=?' for k in f)} WHERE id=?", (*f.values(), rid))
            c.commit()
    return {"ok": True}

@app.delete("/api/receipts/{rid}")
def delete_receipt(rid: int, user=Depends(current_user)):
    with closing(db()) as c:
        r = own(c, rid, user["id"])
        for fn in (r["filename"], r["thumb"]):
            if fn:
                (UPLOADS / fn).unlink(missing_ok=True)
        c.execute("DELETE FROM receipts WHERE id=?", (rid,))
        c.commit()
    return {"ok": True}

class DeadlineIn(BaseModel):
    kind: str = "other"
    label: str = ""
    date: str

@app.post("/api/receipts/{rid}/deadlines")
def add_deadline(rid: int, body: DeadlineIn, user=Depends(current_user)):
    if not valid_date(body.date):
        raise HTTPException(400, "Date must be YYYY-MM-DD")
    with closing(db()) as c:
        own(c, rid, user["id"])
        c.execute("INSERT INTO deadlines(receipt_id,kind,label,date) VALUES(?,?,?,?)",
                  (rid, body.kind if body.kind in KINDS else "other", body.label[:200], body.date))
        c.commit()
    threading.Thread(target=check_alerts, daemon=True).start()
    return {"ok": True}

@app.delete("/api/deadlines/{did}")
def delete_deadline(did: int, user=Depends(current_user)):
    with closing(db()) as c:
        c.execute("""DELETE FROM deadlines WHERE id=? AND receipt_id IN
                     (SELECT id FROM receipts WHERE user_id=?)""", (did, user["id"]))
        c.commit()
    return {"ok": True}

@app.get("/api/summary")
def summary(user=Depends(current_user)):
    month = date.today().strftime("%Y-%m")
    with closing(db()) as c:
        rows = c.execute("""SELECT COALESCE(currency,'') cur, ROUND(SUM(total),2) total FROM receipts
                            WHERE user_id=? AND total IS NOT NULL
                              AND substr(COALESCE(purchase_date, created_at),1,7)=? GROUP BY cur""",
                         (user["id"], month)).fetchall()
    return {"month": month, "totals": [dict(r) for r in rows]}

@app.get("/healthz")
def healthz():
    return {"ok": True}

app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

@app.get("/")
def index():
    return FileResponse(BASE / "static" / "index.html", headers={"Cache-Control": "no-cache"})
