import os, sqlite3, secrets, hashlib, hmac, time, json, subprocess, tempfile, shutil, asyncio
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import FastAPI, HTTPException, Depends, Header, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
import edge_tts

APP_NAME = "Movie Recap Studio"
DB_PATH = os.getenv("DATABASE_PATH", "data/app.db")
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
PAYMENT_DIR = DATA_DIR / "payments"
WORK_DIR = DATA_DIR / "work"
for folder in (DATA_DIR, PAYMENT_DIR, WORK_DIR):
    folder.mkdir(parents=True, exist_ok=True)

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@example.com").lower().strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SESSION_HOURS = int(os.getenv("SESSION_HOURS", "72"))
PREMIUM_GEMINI_API_KEY = os.getenv("PREMIUM_GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
MAX_PAYMENT_BYTES = 5 * 1024 * 1024
MAX_VIDEO_BYTES = 500 * 1024 * 1024
MAX_AUDIO_BYTES = 100 * 1024 * 1024

app = FastAPI(title=APP_NAME, version="2.1.0")
origins = [x.strip() for x in os.getenv("CORS_ORIGINS", "*").split(",") if x.strip()]
app.add_middleware(CORSMiddleware, allow_origins=origins or ["*"],
                   allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

FEATURE_DEFAULTS = {
    "movie_recap": 1, "tts": 1, "translation": 1, "subtitles": 1,
    "blur": 1, "video_export": 1, "project_save": 1, "voice_clone": 0,
    "maintenance": 0
}

def now():
    return datetime.now(timezone.utc).isoformat()

def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def hash_password(pw: str):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 310000)
    return salt.hex() + ":" + dk.hex()

def verify_password(pw, stored):
    try:
        salt, expected = stored.split(":")
        actual = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 310000).hex()
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False

def token_digest(token):
    return hashlib.sha256(token.encode()).hexdigest()

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, display_name TEXT NOT NULL,
          password_hash TEXT NOT NULL, own_api_key TEXT, premium_until TEXT,
          is_active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions(
          token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS payments(
          id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
          phone TEXT NOT NULL, txn_last4 TEXT NOT NULL, screenshot TEXT,
          status TEXT NOT NULL DEFAULT 'pending', admin_note TEXT,
          created_at TEXT NOT NULL, reviewed_at TEXT
        );
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS projects(
          id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
          name TEXT NOT NULL, content TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE(user_id,name)
        );
        CREATE TABLE IF NOT EXISTS audit(
          id INTEGER PRIMARY KEY, actor_id INTEGER, action TEXT NOT NULL,
          detail TEXT, created_at TEXT NOT NULL
        );
        """)
        for k, v in FEATURE_DEFAULTS.items():
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (f"feature:{k}", str(v)))
        defaults = {"wavepay_phone":"", "wavepay_name":"", "wavepay_qr_path":"",
                    "premium_price":"", "premium_days":"30"}
        for k, v in defaults.items():
            c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
        if ADMIN_PASSWORD:
            row = c.execute("SELECT id FROM users WHERE email=?", (ADMIN_EMAIL,)).fetchone()
            if not row:
                c.execute("INSERT INTO users(email,display_name,password_hash,created_at) VALUES(?,?,?,?)",
                          (ADMIN_EMAIL, "Administrator", hash_password(ADMIN_PASSWORD), now()))

def audit(actor, action, detail=""):
    with db() as c:
        c.execute("INSERT INTO audit(actor_id,action,detail,created_at) VALUES(?,?,?,?)",
                  (actor, action, detail, now()))

def setting(k, default=""):
    with db() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
        return r["value"] if r else default

def set_setting(k, v):
    with db() as c:
        c.execute("""INSERT INTO settings(key,value) VALUES(?,?)
                     ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (k, str(v)))

def feature_on(name):
    return setting(f"feature:{name}", str(FEATURE_DEFAULTS.get(name, 0))) == "1"

def require_feature(name):
    if feature_on("maintenance"):
        raise HTTPException(503, "ဝက်ဘ်ဆိုဒ်ကို ခဏပြုပြင်ထိန်းသိမ်းနေပါတယ်။")
    if not feature_on(name):
        raise HTTPException(403, f"{name} ကို Admin က ယာယီပိတ်ထားပါတယ်။")

def current_user(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Login ဝင်ပါ။")
    token = authorization[7:]
    with db() as c:
        r = c.execute("""SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id
                         WHERE s.token_hash=? AND s.expires_at>?""",
                      (token_digest(token), int(time.time()))).fetchone()
    if not r or not r["is_active"]:
        raise HTTPException(401, "Session သက်တမ်းကုန်သွားပါပြီ။")
    return dict(r)

def admin_user(u=Depends(current_user)):
    if u["email"] != ADMIN_EMAIL:
        raise HTTPException(403, "Admin သာ အသုံးပြုနိုင်ပါတယ်။")
    return u

def is_premium(u):
    if not u.get("premium_until"):
        return False
    try:
        until = datetime.fromisoformat(u["premium_until"])
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return until > datetime.now(timezone.utc)
    except Exception:
        return False

def public_user(u):
    return {"id":u["id"], "email":u["email"], "display_name":u["display_name"],
            "premium":is_premium(u), "premium_until":u.get("premium_until"), "is_admin":u.get("email")==ADMIN_EMAIL}

def resolve_gemini_key(u, user_key=None):
    if is_premium(u):
        if not PREMIUM_GEMINI_API_KEY:
            raise HTTPException(503, "Premium API Key ကို Admin က မသတ်မှတ်ရသေးပါ။")
        return PREMIUM_GEMINI_API_KEY
    key = (user_key or "").strip()
    if not key:
        raise HTTPException(400, "Free အသုံးပြုသူဖြစ်သောကြောင့် ကိုယ်ပိုင် Gemini API Key ထည့်ပါ။")
    return key

class RegisterIn(BaseModel):
    email: str
    display_name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=8, max_length=128)
class LoginIn(BaseModel):
    email: str
    password: str
class PaymentReview(BaseModel):
    approve: bool
    note: str = ""
class PremiumToggle(BaseModel):
    enabled: bool
    days: int = Field(default=30, ge=1, le=365)
class FeatureToggle(BaseModel):
    enabled: bool
class SettingsIn(BaseModel):
    wavepay_phone: str = ""
    wavepay_name: str = ""
    premium_price: str = ""
    premium_days: str = "30"

init_db()

@app.get("/api/health")
def health():
    return {"ok": True, "app": APP_NAME, "version": app.version}

@app.post("/api/register")
def register(data: RegisterIn):
    email = data.email.lower().strip()
    if "@" not in email:
        raise HTTPException(400, "Email မှန်ကန်စွာထည့်ပါ။")
    try:
        with db() as c:
            cur = c.execute("INSERT INTO users(email,display_name,password_hash,created_at) VALUES(?,?,?,?)",
                            (email, data.display_name.strip(), hash_password(data.password), now()))
            uid = cur.lastrowid
    except sqlite3.IntegrityError:
        raise HTTPException(409, "ဒီ Email နဲ့ အကောင့်ရှိပြီးသားပါ။")
    return {"ok": True, "user_id": uid}

@app.post("/api/login")
def login(data: LoginIn):
    with db() as c:
        u = c.execute("SELECT * FROM users WHERE email=?", (data.email.lower().strip(),)).fetchone()
    if not u or not verify_password(data.password, u["password_hash"]):
        raise HTTPException(401, "Email သို့မဟုတ် Password မှားနေပါတယ်။")
    token = secrets.token_urlsafe(36)
    with db() as c:
        c.execute("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)",
                  (token_digest(token), u["id"], int(time.time()) + SESSION_HOURS * 3600))
    return {"token": token, "user": public_user(dict(u))}

@app.post("/api/logout")
def logout(u=Depends(current_user), authorization: str=Header(...)):
    with db() as c:
        c.execute("DELETE FROM sessions WHERE token_hash=?", (token_digest(authorization[7:]),))
    return {"ok": True}

@app.get("/api/me")
def me(u=Depends(current_user)):
    return public_user(u)

@app.get("/api/config")
def config():
    return {"features":{k:feature_on(k) for k in FEATURE_DEFAULTS},
            "wavepay":{"phone":setting("wavepay_phone"), "name":setting("wavepay_name"),
                       "qr_available":bool(setting("wavepay_qr_path"))},
            "premium_price":setting("premium_price"), "premium_days":setting("premium_days")}

@app.post("/api/payment/submit")
async def submit_payment(phone:str=Form(...), txn_last4:str=Form(...),
                         screenshot:UploadFile=File(...), u=Depends(current_user)):
    phone, last4 = phone.strip(), txn_last4.strip()
    if not phone or len(phone) > 30:
        raise HTTPException(400, "WavePay ဖုန်းနံပါတ် မှန်ကန်စွာထည့်ပါ။")
    if len(last4) != 4 or not last4.isdigit():
        raise HTTPException(400, "Transaction နောက်ဆုံးဂဏန်း ၄ လုံး ထည့်ပါ။")
    if screenshot.content_type not in ("image/jpeg", "image/png", "image/webp"):
        raise HTTPException(400, "JPG, PNG, WEBP ပုံသာ တင်နိုင်ပါတယ်။")
    content = await screenshot.read()
    if len(content) > MAX_PAYMENT_BYTES:
        raise HTTPException(413, "Screenshot 5MB ထက် မကျော်ရပါ။")
    ext = {"image/jpeg":".jpg", "image/png":".png", "image/webp":".webp"}[screenshot.content_type]
    filename = f"{u['id']}_{secrets.token_hex(12)}{ext}"
    (PAYMENT_DIR / filename).write_bytes(content)
    with db() as c:
        cur = c.execute("""INSERT INTO payments(user_id,phone,txn_last4,screenshot,created_at)
                           VALUES(?,?,?,?,?)""", (u["id"], phone, last4, filename, now()))
        pid = cur.lastrowid
    audit(u["id"], "payment_submitted", f"payment_id={pid}")
    return {"ok":True, "payment_id":pid, "status":"pending",
            "message":"ငွေပေးချေမှုအချက်အလက် Admin ထံတင်ပြီးပါပြီ။"}

@app.get("/api/payment/status")
def payment_status(u=Depends(current_user)):
    with db() as c:
        rows = c.execute("""SELECT id,phone,txn_last4,status,admin_note,created_at,reviewed_at
                            FROM payments WHERE user_id=? ORDER BY id DESC""", (u["id"],)).fetchall()
    return [dict(x) for x in rows]

@app.get("/api/admin/overview")
def admin_overview(a=Depends(admin_user)):
    with db() as c:
        users = c.execute("""SELECT id,email,display_name,premium_until,is_active,created_at
                             FROM users ORDER BY id DESC""").fetchall()
        payments = c.execute("""SELECT p.*,u.email,u.display_name FROM payments p
                                JOIN users u ON u.id=p.user_id
                                ORDER BY CASE p.status WHEN 'pending' THEN 0 ELSE 1 END,p.id DESC""").fetchall()
    return {"users":[dict(x) for x in users], "payments":[dict(x) for x in payments],
            "features":{k:feature_on(k) for k in FEATURE_DEFAULTS},
            "wavepay":{"phone":setting("wavepay_phone"), "name":setting("wavepay_name"),
                       "premium_price":setting("premium_price"), "premium_days":setting("premium_days")}}

@app.get("/api/admin/payment/{payment_id}/screenshot")
def payment_screenshot(payment_id:int, a=Depends(admin_user)):
    with db() as c:
        p = c.execute("SELECT screenshot FROM payments WHERE id=?", (payment_id,)).fetchone()
    if not p or not p["screenshot"]:
        raise HTTPException(404, "Screenshot မတွေ့ပါ။")
    path = PAYMENT_DIR / p["screenshot"]
    if not path.exists():
        raise HTTPException(404, "Screenshot ဖိုင်မရှိတော့ပါ။")
    return FileResponse(path)

@app.post("/api/admin/payment/{payment_id}/review")
def review_payment(payment_id:int, data:PaymentReview, a=Depends(admin_user)):
    with db() as c:
        p = c.execute("SELECT * FROM payments WHERE id=?", (payment_id,)).fetchone()
        if not p:
            raise HTTPException(404, "Payment မတွေ့ပါ။")
        if p["status"] != "pending":
            raise HTTPException(409, "ဒီ Payment ကို စစ်ဆေးပြီးသားပါ။")
        status = "approved" if data.approve else "rejected"
        c.execute("UPDATE payments SET status=?,admin_note=?,reviewed_at=? WHERE id=?",
                  (status, data.note.strip(), now(), payment_id))
        if data.approve:
            days = int(setting("premium_days", "30"))
            until = datetime.now(timezone.utc) + timedelta(days=days)
            c.execute("UPDATE users SET premium_until=? WHERE id=?", (until.isoformat(), p["user_id"]))
    audit(a["id"], "payment_reviewed", f"payment_id={payment_id};status={status}")
    return {"ok":True, "status":status}

@app.post("/api/admin/user/{user_id}/premium")
def toggle_premium(user_id:int, data:PremiumToggle, a=Depends(admin_user)):
    with db() as c:
        u = c.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone()
        if not u:
            raise HTTPException(404, "User မတွေ့ပါ။")
        until = (datetime.now(timezone.utc) + timedelta(days=data.days)).isoformat() if data.enabled else None
        c.execute("UPDATE users SET premium_until=? WHERE id=?", (until, user_id))
    audit(a["id"], "premium_toggled", f"user_id={user_id};enabled={data.enabled}")
    return {"ok":True, "premium":data.enabled, "premium_until":until}

@app.post("/api/admin/feature/{name}")
def toggle_feature(name:str, data:FeatureToggle, a=Depends(admin_user)):
    if name not in FEATURE_DEFAULTS:
        raise HTTPException(404, "Feature မတွေ့ပါ။")
    set_setting(f"feature:{name}", 1 if data.enabled else 0)
    audit(a["id"], "feature_toggled", f"{name}={data.enabled}")
    return {"ok":True, "feature":name, "enabled":data.enabled}

@app.post("/api/admin/settings/wavepay")
def wavepay_settings(data:SettingsIn, a=Depends(admin_user)):
    set_setting("wavepay_phone", data.wavepay_phone.strip())
    set_setting("wavepay_name", data.wavepay_name.strip())
    set_setting("premium_price", data.premium_price.strip())
    set_setting("premium_days", str(max(1, min(365, int(data.premium_days or "30")))))
    audit(a["id"], "wavepay_settings_updated")
    return {"ok":True}

@app.post("/api/admin/settings/wavepay-qr")
async def upload_wavepay_qr(file:UploadFile=File(...), a=Depends(admin_user)):
    if file.content_type not in ("image/jpeg", "image/png", "image/webp"):
        raise HTTPException(400, "Image ဖိုင်သာ တင်ပါ။")
    data = await file.read()
    if len(data) > MAX_PAYMENT_BYTES:
        raise HTTPException(413, "5MB ထက်မကျော်ရပါ။")
    ext = {"image/jpeg":".jpg", "image/png":".png", "image/webp":".webp"}[file.content_type]
    name = "wavepay_qr" + ext
    (DATA_DIR / name).write_bytes(data)
    set_setting("wavepay_qr_path", name)
    return {"ok":True}

@app.get("/api/wavepay/qr")
def wavepay_qr():
    name = setting("wavepay_qr_path")
    if not name:
        raise HTTPException(404, "WavePay QR မထည့်ရသေးပါ။")
    path = DATA_DIR / name
    if not path.exists():
        raise HTTPException(404, "QR ဖိုင်မတွေ့ပါ။")
    return FileResponse(path)

@app.post("/api/projects")
def save_project(name:str=Form(...), content:str=Form(...), u=Depends(current_user)):
    require_feature("project_save")
    if len(content) > 2_000_000:
        raise HTTPException(413, "Project 2MB ထက် မကျော်ရပါ။")
    with db() as c:
        c.execute("""INSERT INTO projects(user_id,name,content,updated_at) VALUES(?,?,?,?)
                     ON CONFLICT(user_id,name) DO UPDATE SET content=excluded.content,updated_at=excluded.updated_at""",
                  (u["id"], name[:100], content, now()))
    return {"ok":True}

@app.get("/api/projects")
def list_projects(u=Depends(current_user)):
    require_feature("project_save")
    with db() as c:
        rows = c.execute("SELECT id,name,updated_at FROM projects WHERE user_id=? ORDER BY updated_at DESC",
                         (u["id"],)).fetchall()
    return [dict(x) for x in rows]

@app.get("/api/projects/{project_id}")
def load_project(project_id:int, u=Depends(current_user)):
    require_feature("project_save")
    with db() as c:
        p = c.execute("SELECT id,name,content,updated_at FROM projects WHERE id=? AND user_id=?",
                      (project_id, u["id"])).fetchone()
    if not p:
        raise HTTPException(404, "Project မတွေ့ပါ။")
    return dict(p)

@app.delete("/api/projects/{project_id}")
def delete_project(project_id:int, u=Depends(current_user)):
    with db() as c:
        c.execute("DELETE FROM projects WHERE id=? AND user_id=?", (project_id, u["id"]))
    return {"ok":True}

@app.post("/api/key/check")
def key_check(user_key:str=Form(""), u=Depends(current_user)):
    resolve_gemini_key(u, user_key)
    return {"ok":True, "key_source":"server_premium" if is_premium(u) else "user_free"}

@app.get("/api/admin/audit")
def admin_audit(a=Depends(admin_user)):
    with db() as c:
        rows = c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 200").fetchall()
    return [dict(x) for x in rows]


@app.post("/api/recap")
async def recap(video: UploadFile = File(...), api_key: str = Form(""),
                language: str = Form("မြန်မာ"), style: str = Form("စိတ်ဝင်စားဖွယ် YouTube Movie Recap"),
                extra: str = Form(""), u=Depends(current_user)):
    require_feature("movie_recap")
    key = resolve_gemini_key(u, api_key)
    if video.content_type not in ("video/mp4", "video/quicktime", "video/webm", "video/x-matroska"):
        raise HTTPException(400, "MP4/MOV/WEBM/MKV Video ဖိုင်သာ တင်ပါ။")
    raw = await video.read()
    if len(raw) > MAX_VIDEO_BYTES:
        raise HTTPException(413, "Video ဖိုင် 500MB ထက်ကျော်နေပါတယ်။")
    import httpx
    mime = video.content_type
    async with httpx.AsyncClient(timeout=240) as client:
        start = await client.post(
            "https://generativelanguage.googleapis.com/upload/v1beta/files",
            headers={"x-goog-api-key":key, "X-Goog-Upload-Protocol":"resumable",
                     "X-Goog-Upload-Command":"start", "X-Goog-Upload-Header-Content-Length":str(len(raw)),
                     "X-Goog-Upload-Header-Content-Type":mime, "Content-Type":"application/json"},
            json={"file":{"display_name":Path(video.filename or "movie.mp4").name}})
        if start.status_code >= 400:
            raise HTTPException(502, "Gemini Upload မအောင်မြင်ပါ: "+start.text[:500])
        upload_url = start.headers.get("x-goog-upload-url")
        if not upload_url:
            raise HTTPException(502, "Gemini Upload URL မရပါ။")
        up = await client.post(upload_url, headers={"x-goog-upload-command":"upload, finalize",
                                                    "x-goog-upload-offset":"0"}, content=raw)
        if up.status_code >= 400:
            raise HTTPException(502, "Gemini Upload မအောင်မြင်ပါ: "+up.text[:500])
        file_data = up.json().get("file", up.json())
        fname = file_data.get("name")
        if not fname:
            raise HTTPException(502, "Gemini File ID မရပါ။")
        for _ in range(60):
            if file_data.get("state") == "ACTIVE":
                break
            if file_data.get("state") == "FAILED":
                raise HTTPException(502, "Gemini Video processing မအောင်မြင်ပါ။")
            await asyncio.sleep(3)
            chk = await client.get("https://generativelanguage.googleapis.com/v1beta/"+fname,
                                   headers={"x-goog-api-key":key})
            if chk.status_code >= 400:
                raise HTTPException(502, "Gemini Video status မစစ်နိုင်ပါ။")
            file_data = chk.json()
        else:
            raise HTTPException(504, "Video processing ကြာလွန်းနေပါတယ်။")
        prompt = (f"Analyze the full movie clip and write a natural {language} narration in this style: {style}. "
                  "Preserve the plot order, character names when clear, actions and important dialogue meaning. "
                  "Narrate dialogue indirectly in one narrator voice. Do not invent scenes, motives or lines. "
                  "Return narration text only. Extra instructions: "+extra[:3000])
        result = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
            headers={"x-goog-api-key":key, "Content-Type":"application/json"},
            json={"contents":[{"parts":[{"file_data":{"mime_type":mime,"file_uri":file_data.get("uri")}},
                                        {"text":prompt}]}],
                  "generationConfig":{"temperature":0.35}})
        if result.status_code >= 400:
            raise HTTPException(502, "Gemini Recap မအောင်မြင်ပါ: "+result.text[:600])
        parts = result.json().get("candidates",[{}])[0].get("content",{}).get("parts",[])
        text_out = "\n".join(p.get("text","") for p in parts if p.get("text")).strip()
        if not text_out:
            raise HTTPException(502, "Gemini က Script မပြန်ပေးပါ။")
        return {"text":text_out, "key_source":"server_premium" if is_premium(u) else "user_free"}

@app.post("/api/tts")
async def tts(text:str=Form(...), voice:str=Form("my-MM-ThihaNeural"),
              rate:str=Form("+10%"), u=Depends(current_user)):
    require_feature("tts")
    if len(text.strip()) == 0 or len(text) > 100000:
        raise HTTPException(400, "TTS စာသား မမှန်ပါ။")
    if voice not in ("my-MM-ThihaNeural", "my-MM-NilarNeural"):
        raise HTTPException(400, "Voice မမှန်ပါ။")
    if rate not in ("+0%", "+5%", "+10%", "+15%", "+20%", "-5%", "-10%"):
        raise HTTPException(400, "TTS speed မမှန်ပါ။")
    name = WORK_DIR / f"tts_{secrets.token_hex(10)}.mp3"
    try:
        communicate = edge_tts.Communicate(text, voice, rate=rate)
        await communicate.save(str(name))
        if not name.exists() or name.stat().st_size == 0:
            raise HTTPException(502, "TTS အသံဖိုင် မထွက်လာပါ။")
        return FileResponse(name, media_type="audio/mpeg", filename="movie-recap.mp3",
                            background=__import__("starlette.background").background.BackgroundTask(lambda: name.unlink(missing_ok=True)))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"TTS မအောင်မြင်ပါ: {str(e)[:300]}")

@app.post("/api/auto-dub")
async def auto_dub(video:UploadFile=File(...), api_key:str=Form(""),
                   voice:str=Form("my-MM-ThihaNeural"), audio_speed:float=Form(1.1),
                   language:str=Form("မြန်မာ"), u=Depends(current_user)):
    require_feature("translation")
    require_feature("tts")
    key = resolve_gemini_key(u, api_key)
    if video.content_type not in ("video/mp4", "video/quicktime", "video/webm", "video/x-matroska"):
        raise HTTPException(400, "MP4/MOV/WEBM/MKV Video ဖိုင်သာ တင်ပါ။")
    raw = await video.read()
    if len(raw) > MAX_VIDEO_BYTES:
        raise HTTPException(413, "Video ဖိုင် 500MB ထက်ကျော်နေပါတယ်။")
    if voice not in ("my-MM-ThihaNeural", "my-MM-NilarNeural"):
        raise HTTPException(400, "Voice မမှန်ပါ။")
    if not 1.0 <= audio_speed <= 1.5:
        raise HTTPException(400, "အသံမြန်နှုန်း 1.0× မှ 1.5× အတွင်းထားပါ။")
    work = Path(tempfile.mkdtemp(prefix="dub_", dir=WORK_DIR))
    source = work / ("input" + (Path(video.filename or "video.mp4").suffix or ".mp4"))
    source.write_bytes(raw)
    try:
        # Use Gemini Files API, then generate structured narration segments.
        import httpx
        mime = video.content_type
        async with httpx.AsyncClient(timeout=180) as client:
            start = await client.post(
                "https://generativelanguage.googleapis.com/upload/v1beta/files",
                headers={"x-goog-api-key":key, "X-Goog-Upload-Protocol":"resumable",
                         "X-Goog-Upload-Command":"start", "X-Goog-Upload-Header-Content-Length":str(len(raw)),
                         "X-Goog-Upload-Header-Content-Type":mime, "Content-Type":"application/json"},
                json={"file":{"display_name":Path(video.filename or "video.mp4").name}})
            if start.status_code >= 400:
                raise HTTPException(502, "Gemini Video Upload မအောင်မြင်ပါ: " + start.text[:500])
            upload_url = start.headers.get("x-goog-upload-url")
            if not upload_url:
                raise HTTPException(502, "Gemini Upload URL မရပါ။")
            up = await client.post(upload_url, headers={"x-goog-upload-command":"upload, finalize",
                                                        "x-goog-upload-offset":"0"}, content=raw)
            if up.status_code >= 400:
                raise HTTPException(502, "Gemini Upload မအောင်မြင်ပါ: " + up.text[:500])
            fdata = up.json().get("file", up.json())
            fname = fdata.get("name")
            if not fname:
                raise HTTPException(502, "Gemini File ID မရပါ။")
            # Wait for processing.
            for _ in range(60):
                state = fdata.get("state")
                if state == "ACTIVE":
                    break
                if state == "FAILED":
                    raise HTTPException(502, "Gemini Video processing မအောင်မြင်ပါ။")
                await asyncio.sleep(3)
                chk = await client.get("https://generativelanguage.googleapis.com/v1beta/" + fname,
                                       headers={"x-goog-api-key":key})
                if chk.status_code >= 400:
                    raise HTTPException(502, "Gemini Video အခြေအနေ မစစ်နိုင်ပါ။")
                fdata = chk.json()
            else:
                raise HTTPException(504, "Gemini Video processing ကြာလွန်းနေပါတယ်။")
            prompt = (
                f"Analyze this entire video and produce faithful {language} narration segments for a movie recap. "
                "Do not invent events, motives, dialogue, or names. Narrate dialogue indirectly in a single narrator voice. "
                "Keep chronological order and cover all important visible story beats. Return JSON only: "
                '{"segments":[{"start":0.0,"end":5.0,"source_text":"short source meaning","text":"natural Burmese narration"}]}. '
                "Each segment should be concise and have valid non-overlapping time ranges. Do not omit the ending."
            )
            gen = await client.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
                headers={"x-goog-api-key":key, "Content-Type":"application/json"},
                json={"contents":[{"parts":[{"file_data":{"mime_type":mime,"file_uri":fdata.get("uri")}},
                                            {"text":prompt}]}],
                      "generationConfig":{"responseMimeType":"application/json","temperature":0.3}})
            if gen.status_code >= 400:
                raise HTTPException(502, "Gemini Recap မအောင်မြင်ပါ: " + gen.text[:600])
            payload = gen.json()
        parts = payload.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text_out = "".join(x.get("text", "") for x in parts if x.get("text"))
        try:
            parsed = json.loads(text_out)
            segments = parsed.get("segments", [])
        except Exception:
            raise HTTPException(502, "Gemini က JSON Segment မမှန်ကန်စွာ ပြန်ပေးပါတယ်။")
        if not segments:
            raise HTTPException(502, "ဇာတ်လမ်း Segment မရရှိပါ။")
        # Generate one continuous MP3, preserving the whole script.
        full_text = " ".join(str(x.get("text", "")).strip() for x in segments if x.get("text"))
        if not full_text:
            raise HTTPException(502, "မြန်မာဇာတ်ကြောင်းစာသား မရရှိပါ။")
        mp3 = work / "dub.mp3"
        await edge_tts.Communicate(full_text, voice, rate=f"{round((audio_speed-1)*100):+d}%").save(str(mp3))
        return {"segments":segments, "audio_base64":__import__("base64").b64encode(mp3.read_bytes()).decode(),
                "warnings":["Video playback speed ကို 1.0× အတိုင်းထားပါသည်။ အသံ speed ကိုသာ ရွေးထားသည့်နှုန်းဖြင့် ပြောင်းထားသည်။"]}
    finally:
        shutil.rmtree(work, ignore_errors=True)

@app.post("/api/render-mp4")
async def render_mp4(video:UploadFile=File(...), audio:UploadFile=File(...),
                     blur_regions:str=Form("[]"), subtitles:str=Form("[]"),
                     audio_offset:float=Form(0), audio_tempo:float=Form(1.0),
                     u=Depends(current_user)):
    require_feature("video_export")
    require_feature("blur")
    require_feature("subtitles")
    vdata, adata = await video.read(), await audio.read()
    if len(vdata) > MAX_VIDEO_BYTES or len(adata) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "ဖိုင်အရွယ်အစား ကန့်သတ်ချက် ကျော်နေပါတယ်။")
    try:
        blurs = json.loads(blur_regions or "[]")
        subs = json.loads(subtitles or "[]")
        if not isinstance(blurs, list) or not isinstance(subs, list):
            raise ValueError()
    except Exception:
        raise HTTPException(400, "Blur/Subtitles JSON မမှန်ပါ။")
    if len(blurs) > 30 or len(subs) > 500:
        raise HTTPException(400, "Blur သို့မဟုတ် Subtitle အရေအတွက် များလွန်းပါတယ်။")
    work = Path(tempfile.mkdtemp(prefix="render_", dir=WORK_DIR))
    inp, aud, out = work/"input.mp4", work/"voice.mp3", work/"output.mp4"
    inp.write_bytes(vdata); aud.write_bytes(adata)
    try:
        # Get source resolution and duration.
        probe = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration",
                                "-show_entries","stream=width,height","-of","json",str(inp)],
                               capture_output=True, text=True, timeout=60)
        if probe.returncode:
            raise HTTPException(400, "Video ဖိုင်ကို ဖတ်မရပါ။ FFmpeg/ffprobe စစ်ပါ။")
        info = json.loads(probe.stdout)
        streams = [s for s in info.get("streams",[]) if s.get("width") and s.get("height")]
        if not streams:
            raise HTTPException(400, "Video stream မတွေ့ပါ။")
        width, height = int(streams[0]["width"]), int(streams[0]["height"])
        duration = float(info.get("format",{}).get("duration",0) or 0)
        filters = []
        current = "[0:v]"
        for i,b in enumerate(blurs):
            x=max(0,min(width-2,int(float(b.get("x",0))*width)))
            y=max(0,min(height-2,int(float(b.get("y",0))*height)))
            w=max(2,min(width-x,int(float(b.get("width",.1))*width)))
            h=max(2,min(height-y,int(float(b.get("height",.1))*height)))
            radius=max(1,min(30,int(float(b.get("strength",50))/5)))
            nxt=f"[v{i}]"
            filters.append(f"{current}split[base{i}][cropin{i}];[cropin{i}]crop={w}:{h}:{x}:{y},boxblur={radius}:2[blur{i}];[base{i}][blur{i}]overlay={x}:{y}{nxt}")
            current=nxt
        for i,s in enumerate(subs):
            txt=str(s.get("text","")).replace("\\","\\\\").replace(":","\\:").replace("'","\\'").replace("%","\\%").replace(",","\\,")
            start=max(0,float(s.get("start",0))); end=max(start+.1,float(s.get("end",duration or 1)))
            size=max(12,min(100,int(s.get("size",30))))
            bottom=max(0,min(90,float(s.get("bottom",7))))
            yexpr=f"h-text_h-h*{bottom/100:.3f}"
            nxt=f"[sub{i}]"
            filters.append(f"{current}drawtext=text='{txt}':enable='between(t,{start:.3f},{end:.3f})':font='Noto Sans Myanmar':fontsize={size}:fontcolor=yellow:borderw=3:bordercolor=black:x=(w-text_w)/2:y={yexpr}{nxt}")
            current=nxt
        filter_complex=";".join(filters) if filters else ""
        cmd=["ffmpeg","-y","-i",str(inp),"-ss","0","-itsoffset",str(max(0,audio_offset)),"-i",str(aud)]
        if filter_complex:
            cmd += ["-filter_complex",filter_complex,"-map",current,"-map","1:a:0"]
        else:
            cmd += ["-map","0:v:0","-map","1:a:0"]
        tempo=max(.5,min(2.0,float(audio_tempo)))
        cmd += ["-filter:a",f"atempo={tempo}","-c:v","libx264","-preset","veryfast","-crf","20",
                "-c:a","aac","-b:a","192k","-pix_fmt","yuv420p","-movflags","+faststart",
                "-shortest",str(out)]
        run=subprocess.run(cmd,capture_output=True,text=True,timeout=3600)
        if run.returncode or not out.exists() or out.stat().st_size==0:
            raise HTTPException(500, "MP4 Export မအောင်မြင်ပါ: "+(run.stderr[-1500:] if run.stderr else "FFmpeg error"))
        return FileResponse(out,media_type="video/mp4",filename="movie-recap.mp4",
                            background=__import__("starlette.background").background.BackgroundTask(lambda: shutil.rmtree(work,ignore_errors=True)))
    except HTTPException:
        shutil.rmtree(work,ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(work,ignore_errors=True)
        raise HTTPException(500, f"Export error: {str(e)[:500]}")
