#!/usr/bin/env python3
"""ThumbPack — STANDALONE deployable service (stdlib only).

Same product as the local Phase-2 gateway (accounts, per-day quotas,
Lemon Squeezy webhook), but generation happens INSIDE this app: it calls
Google Gemini's OpenAI-compatible endpoint directly — no n8n, no FreeLLM,
no dependency on any other machine. Built for Hugging Face Spaces
(Docker), but runs anywhere Python 3 runs.

Environment:
  GEMINI_API_KEY  (required for generation; without it /api/generate
                   returns 503 {"error":"ai_not_configured"})
  GEMINI_MODEL    (default "gemini-3.5-flash" — verified live against
                   Rony's key 2026-10-21 via the NATIVE generateContent
                   API: gemini-3.5-flash and gemini-3.5-flash-lite answer,
                   gemini-3.8-flash exists but is often busy, and all
                   2.x names return 404 for this key)
  PORT            (default: config.json "port", else 7860) — binds 0.0.0.0

The prompt text and the strict-JSON response schema are byte-for-byte the
ones proven in the n8n workflow (see ../build-workflow.py).
"""
import base64
import hashlib
import hmac
import json
import os
import random
import secrets
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
DB_PATH = os.path.join(BASE, "thumbpack.db")
PAYMENTS_LOG = os.path.join(BASE, "payments.log")
QUOTAS = {"free": 5, "pro": 200}
PBKDF2_ROUNDS = 200_000
MAX_BODY = 12 * 1024 * 1024
UPLOAD_DIR = os.path.join(BASE, "uploads")
PHOTO_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            pw_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            plan TEXT NOT NULL DEFAULT 'free',
            created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS usage(
            user_id INTEGER NOT NULL,
            day TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(user_id, day));
        CREATE TABLE IF NOT EXISTS showcase(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            best_title TEXT NOT NULL,
            image_url TEXT NOT NULL,
            niche TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL);
    """)
    conn.commit()
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(showcase)")]
    if cols and "thumb_text" not in cols:
        conn.execute("ALTER TABLE showcase ADD COLUMN "
                     "thumb_text TEXT NOT NULL DEFAULT ''")
        conn.commit()
    conn.close()


def today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def hash_pw(password, salt_hex):
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt_hex), PBKDF2_ROUNDS
    ).hex()


def used_today(conn, user_id):
    row = conn.execute(
        "SELECT count FROM usage WHERE user_id=? AND day=?", (user_id, today())
    ).fetchone()
    return row["count"] if row else 0


def save_photo(data_url):
    """Decode a base64 image data URL, store it under uploads/, and return
    its public path (/uploads/<name>). Raises ValueError on bad input."""
    if not data_url.startswith("data:") or "," not in data_url:
        raise ValueError("Invalid photo data")
    header, b64 = data_url.split(",", 1)
    mime = header[5:].split(";")[0].strip().lower()
    ext = PHOTO_EXT.get(mime)
    if not ext:
        raise ValueError("Photo must be JPG, PNG or WebP")
    raw = base64.b64decode(b64)
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError("Photo too large (max 8 MB)")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    name = secrets.token_hex(8) + ext
    with open(os.path.join(UPLOAD_DIR, name), "wb") as f:
        f.write(raw)
    return "/uploads/" + name


def log_payment(line):
    with open(PAYMENTS_LOG, "a") as f:
        f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), **line}) + "\n")


# ---------------------------------------------------------------------------
# Generation — direct Gemini call + strict-JSON parse.
# Prompt text and response schema are verbatim ports of the n8n workflow's
# "Prepare" / "Parse Package" Code nodes (../build-workflow.py).
# ---------------------------------------------------------------------------

class AIError(Exception):
    """Generation backend failure → HTTP 502 (after retries)."""


def build_messages(title, niche, platform, thumb_text, style):
    system = (f"You are an elite {platform} thumbnail and title strategist who "
              f"studies viral videos. You ALWAYS reply with strict JSON only - "
              f"no markdown, no code fences, no explanation.")
    person_rule = ("no people - dramatic close-up of the key object/scene only"
                   if style == "No person - object only" else
                   f'one person with a "{style}" facial expression, exaggerated '
                   f"and readable at small size")
    user = f"""A creator is making a {platform} video.
Topic/title draft: "{title}"
Niche: {niche}
Thumbnail person style: {style}
Big thumbnail text the creator plans to overlay later (for context only): "{thumb_text or '(none)'}"

Return ONLY this exact JSON shape:
{{"titles":[{{"title":"...","ctr_score":8.5}},{{"title":"...","ctr_score":7.0}},{{"title":"...","ctr_score":7.5}},{{"title":"...","ctr_score":6.5}},{{"title":"...","ctr_score":8.0}}],"best_title":"...","image_prompt":"...","tags":["tag1","tag2"]}}

Rules:
- titles: exactly 5 DIFFERENT viral title options for this video. Curiosity gap, numbers, stakes, emotion. Max ~60 characters each. No clickbait lies the video cannot pay off.
- ctr_score: number from 1 to 10 (one decimal allowed) = your predicted click-through strength of that title+thumbnail combo for the {niche} niche on {platform}.
- best_title: must be exactly one of the 5 titles (the one with the highest ctr_score).
- image_prompt: ONE English prompt for an AI image generator describing the thumbnail IMAGE. Vivid, specific composition: {person_rule}, bold saturated colors, strong contrast, dramatic lighting, simple uncluttered background, and EMPTY negative space on one side where big text will be added later. Absolutely NO text, words, letters, numbers, logos or watermarks inside the image. Photorealistic, high detail.
- tags: 10 to 15 SEO tags (lowercase phrases, no # symbol) a creator would paste into {platform}."""
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


def call_gemini(api_key, model, messages):
    """Native Gemini generateContent API + model chain with auto-failover
    (2026-10-21). Verified live with Rony's key: gemini-3.5-flash and
    gemini-3.5-flash-lite answer; the older 2.x names return 404 and the
    OpenAI-compat shim stopped working for this key, so we call the native
    endpoint with x-goog-api-key. Each model has its own free quota bucket:
    on 429/503 retry once, then hop; on 404 hop immediately. Returns
    (content, model_used). Per-attempt timeout 45s."""
    models = [model] + [m for m in ("gemini-3.8-flash",
                                    "gemini-3.5-flash-lite")
                        if m != model]
    system = next((m["content"] for m in messages
                   if m.get("role") == "system"), "")
    user = next((m["content"] for m in messages
                 if m.get("role") == "user"), "")
    last = "unknown error"
    for m in models:
        url = ("https://generativelanguage.googleapis.com/v1beta/models/" +
               m + ":generateContent")
        body = json.dumps({
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0.9}}).encode()
        for attempt in range(2):
            req = urllib.request.Request(
                url, data=body, method="POST",
                headers={"Content-Type": "application/json",
                         "x-goog-api-key": api_key})
            try:
                with urllib.request.urlopen(req, timeout=45) as resp:
                    data = json.loads(resp.read())
                parts = (data["candidates"][0]["content"]["parts"])
                text = "".join(p.get("text", "") for p in parts).strip()
                if not text:
                    raise AIError("generation service returned an empty reply")
                return text, m
            except AIError:
                raise
            except urllib.error.HTTPError as e:
                last = f"generation service returned {e.code}"
                if e.code not in (404, 429, 503):
                    raise AIError(last)
                if e.code == 404:
                    break  # model name retired/unknown: hop to the next model
            except Exception as e:
                last = f"generation service unreachable: {e}"
                break  # network-level failure: try the next model
            if attempt < 1:
                time.sleep(6)
    raise AIError(last + " (all models busy — free quota; try again shortly)")


def extract_json(text):
    if not text:
        return None
    s = text.replace("```json", "").replace("```JSON", "").replace("```", "").strip()
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        s = s[i:j + 1]
    try:
        return json.loads(s)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Image providers — multi-provider chain with auto-failover (Rony's order
# 2026-10-21): Cloudflare SDXL (native 1280x720) -> Cloudflare FLUX ->
# Pollinations. When one provider's free quota ends or it errors, the next
# is tried automatically. Generated bytes are hosted by this app under
# /uploads/ so the frontend canvas can always read them same-origin.
# ---------------------------------------------------------------------------

def _img_ext(raw):
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if raw[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    return None


def _save_generated(raw):
    if not _img_ext(raw):
        return None
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    name = "gen-" + secrets.token_hex(8) + _img_ext(raw)
    with open(os.path.join(UPLOAD_DIR, name), "wb") as f:
        f.write(raw)
    return "/uploads/" + name


def _agnes_image(prompt):
    """Agnes AI image API (agnes-image-2.1-flash) — native ~16:9, fast,
    photorealistic. Needs AGNES_API_KEY in the environment."""
    key = (os.environ.get("AGNES_API_KEY") or "").strip()
    if not key:
        return None
    try:
        req = urllib.request.Request(
            "https://apihub.agnes-ai.com/v1/images/generations",
            data=json.dumps({"model": "agnes-image-2.1-flash",
                             "prompt": prompt, "n": 1,
                             "size": "1280x720"}).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + key}, method="POST")
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        d = (data.get("data") or [{}])[0]
        if d.get("b64_json"):
            raw = base64.b64decode(d["b64_json"])
            if _img_ext(raw):
                return raw
        elif d.get("url"):
            r2 = urllib.request.Request(
                d["url"], headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(r2, timeout=60) as resp2:
                raw = resp2.read()
            if _img_ext(raw):
                return raw
    except Exception:
        pass
    return None


def _cf_image(prompt):
    """Cloudflare Workers AI (free daily neurons) — SDXL then FLUX."""
    aid = (os.environ.get("CF_ACCOUNT_ID") or "").strip()
    tok = (os.environ.get("CF_API_TOKEN") or "").strip()
    if not aid or not tok:
        return None
    base = ("https://api.cloudflare.com/client/v4/accounts/" + aid +
            "/ai/run/")
    hdr = {"Content-Type": "application/json",
           "Authorization": "Bearer " + tok}
    try:  # SDXL returns raw PNG bytes at native 1280x720
        req = urllib.request.Request(
            base + "@cf/stabilityai/stable-diffusion-xl-base-1.0",
            data=json.dumps({"prompt": prompt, "width": 1280,
                             "height": 720}).encode(),
            headers=hdr, method="POST")
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
        if _img_ext(raw):
            return raw
    except Exception:
        pass
    try:  # FLUX schnell returns JSON {"result": {"image": base64}}
        req = urllib.request.Request(
            base + "@cf/black-forest-labs/flux-1-schnell",
            data=json.dumps({"prompt": prompt, "steps": 8}).encode(),
            headers=hdr, method="POST")
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        b64 = (data.get("result") or {}).get("image")
        if b64:
            raw = base64.b64decode(b64)
            if _img_ext(raw):
                return raw
    except Exception:
        pass
    return None


def _pollinations_image(prompt, seed):
    url = ("https://image.pollinations.ai/prompt/" +
           urllib.parse.quote(prompt, safe="") +
           f"?width=1280&height=720&nologo=true&seed={seed}")
    try:
        req = urllib.request.Request(url,
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=180) as resp:
            raw = resp.read()
        if _img_ext(raw):
            return raw
    except Exception:
        pass
    return None


def strip_watermark(raw):
    """Pollinations' free tier stamps a small 'pollinations.ai' mark in the
    bottom-right corner (it ignores nologo). Crop the bottom strip off those
    images (Rony's order 2026-10-21); the frontend canvas re-frames to 16:9
    anyway, so nothing of the subject is lost. No-op if Pillow is absent."""
    try:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        im = im.crop((0, 0, im.width, int(im.height * 0.92)))
        out = io.BytesIO()
        im.save(out, "JPEG", quality=88)
        return out.getvalue()
    except Exception:
        return raw


def generate_image(prompt, seed):
    for raw in (_agnes_image(prompt), _cf_image(prompt)):
        if raw:
            saved = _save_generated(raw)
            if saved:
                return saved
    raw = _pollinations_image(prompt, seed)
    if raw:
        saved = _save_generated(strip_watermark(raw))
        if saved:
            return saved
    raise AIError("All image providers failed. Please try again.")


def generate_package(payload, photo_url=""):
    """Returns the package dict (same schema as the n8n webhook response),
    or the AI-parse error dict — mirroring the workflow's behavior.
    With photo_url set (photo mode), the creator's own photo is the
    thumbnail image; Gemini still writes the titles/tags around it."""
    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not api_key:
        return None  # caller maps this to 503 ai_not_configured
    model = (os.environ.get("GEMINI_MODEL") or "gemini-3.5-flash").strip()
    niche = str(payload.get("niche") or "Other").strip()
    title = str(payload.get("title") or "").strip()
    if not title:
        if photo_url:
            title = "Untitled " + niche + " video"
        else:
            raise ValueError("Missing required field: title")
    platform = str(payload.get("platform") or "YouTube").strip()
    thumb_text = str(payload.get("thumb_text") or "").strip()
    style = str(payload.get("style") or "Shocked face").strip()
    seed = random.randint(1, 999999)

    content, model = call_gemini(
        api_key, model,
        build_messages(title, niche, platform, thumb_text, style))
    data = extract_json(content)
    if not data or not isinstance(data.get("titles"), list):
        return {"error": "AI did not return valid JSON. Please try again.",
                "raw": (content or "")[:400]}

    titles = []
    for t in data["titles"][:5]:
        tt = str((t or {}).get("title") or "").strip()
        if not tt:
            continue
        try:
            score = float((t or {}).get("ctr_score"))
        except (TypeError, ValueError):
            score = 5
        titles.append({"title": tt, "ctr_score": max(1.0, min(10.0, score))})
    best = str(data.get("best_title") or "").strip()
    if not best and titles:
        best = max(titles, key=lambda t: t["ctr_score"])["title"]
    image_prompt = str(data.get("image_prompt") or "").strip() or \
        ("YouTube thumbnail about " + title + ", dramatic, bold colors, no text")
    # Realism/dynamic booster (Rony's order 2026-10-21): the free image model
    # drifts to an illustration look without explicit photographic language.
    image_prompt += (", ultra-photorealistic, shot on a DSLR camera, "
                     "hyper-detailed skin texture, dramatic dynamic composition, "
                     "energetic pose, cinematic rim lighting, vibrant saturated "
                     "colors, shallow depth of field, not an illustration, "
                     "not anime, not a cartoon, not CGI")
    tags = [str(t).strip() for t in (data.get("tags") or []) if str(t).strip()][:15]
    if photo_url:
        image_url = photo_url
    else:
        image_url = generate_image(image_prompt, seed)
    return {"titles": titles, "best_title": best, "tags": tags,
            "image_prompt": image_prompt, "image_url": image_url,
            "seed": seed, "platform": platform, "niche": niche,
            "thumb_text": thumb_text, "model": model}


# ---------------------------------------------------------------------------
# HTTP handler — auth, quotas, webhook (unchanged from the Phase-2 app,
# except generation above and PORT/0.0.0.0 binding in main()).
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "ThumbPack/1.0"

    # ---------- helpers ----------
    def send_json(self, code, obj):
        body = json.dumps(obj).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client already gone (long generation) — nothing to send to

    def read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ValueError("body too large")
        return self.rfile.read(n) if n else b""

    def read_json(self):
        raw = self.read_body()
        return json.loads(raw.decode() or "{}")

    def auth_user(self):
        auth = self.headers.get("Authorization") or ""
        if not auth.startswith("Bearer "):
            return None
        conn = db()
        row = conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token=?",
            (auth[7:].strip(),),
        ).fetchone()
        conn.close()
        return row

    # ---------- routes ----------
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            return self.serve_file("index.html", "text/html; charset=utf-8")
        if path == "/api/me":
            user = self.auth_user()
            if not user:
                return self.send_json(401, {"error": "not_logged_in"})
            conn = db()
            used = used_today(conn, user["id"])
            conn.close()
            return self.send_json(200, {
                "email": user["email"], "plan": user["plan"],
                "used_today": used, "quota": QUOTAS.get(user["plan"], 5)})
        if path == "/api/showcase":
            conn = db()
            rows = conn.execute(
                "SELECT title, best_title, image_url, niche, thumb_text, created_at "
                "FROM showcase ORDER BY id DESC LIMIT 3").fetchall()
            conn.close()
            return self.send_json(200, {"items": [dict(r) for r in rows]})
        if path == "/api/config":
            cfg = load_config()
            return self.send_json(200, {
                "checkout_url_pro": cfg.get("lemonsqueezy_checkout_url_pro", "")})
        if path in ("/sample-thumbnail.jpg",):
            return self.serve_file(path.lstrip("/"), "image/jpeg")
        if path.startswith("/uploads/"):
            name = path[len("/uploads/"):]
            ext = os.path.splitext(name)[1].lower()
            ctypes = {".jpg": "image/jpeg", ".png": "image/png",
                      ".webp": "image/webp"}
            full = os.path.join(UPLOAD_DIR, name)
            if (ext in ctypes and os.path.basename(name) == name
                    and os.path.isfile(full)):
                with open(full, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", ctypes[ext])
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            return self.send_json(404, {"error": "not_found"})
        return self.send_json(404, {"error": "not_found"})

    def serve_file(self, name, ctype):
        full = os.path.join(BASE, name)
        if not os.path.isfile(full):
            return self.send_json(404, {"error": "not_found"})
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            if path == "/api/register":
                return self.handle_register()
            if path == "/api/login":
                return self.handle_login()
            if path == "/api/logout":
                return self.handle_logout()
            if path == "/api/generate":
                return self.handle_generate()
            if path == "/webhook/lemonsqueezy":
                return self.handle_lemonsqueezy()
            return self.send_json(404, {"error": "not_found"})
        except ValueError as e:
            return self.send_json(400, {"error": str(e)})
        except Exception as e:  # keep the server alive, surface the error
            return self.send_json(500, {"error": f"server_error: {e}"})

    # ---------- auth ----------
    def make_session(self, conn, user_id):
        token = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO sessions(token, user_id, created_at) VALUES(?,?,?)",
            (token, user_id, datetime.now(timezone.utc).isoformat()))
        conn.commit()
        return token

    def handle_register(self):
        data = self.read_json()
        email = (data.get("email") or "").strip().lower()
        password = data.get("password") or ""
        if "@" not in email or len(password) < 6:
            return self.send_json(400, {"error": "valid email and a 6+ character password required"})
        salt = secrets.token_hex(16)
        conn = db()
        try:
            cur = conn.execute(
                "INSERT INTO users(email, pw_hash, salt, plan, created_at) VALUES(?,?,?,'free',?)",
                (email, hash_pw(password, salt), salt,
                 datetime.now(timezone.utc).isoformat()))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            return self.send_json(409, {"error": "email already registered — please log in"})
        token = self.make_session(conn, cur.lastrowid)
        conn.close()
        return self.send_json(201, {"token": token, "email": email, "plan": "free"})

    def handle_login(self):
        data = self.read_json()
        email = (data.get("email") or "").strip().lower()
        password = data.get("password") or ""
        conn = db()
        row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not row or not hmac.compare_digest(row["pw_hash"], hash_pw(password, row["salt"])):
            conn.close()
            return self.send_json(401, {"error": "wrong email or password"})
        token = self.make_session(conn, row["id"])
        conn.close()
        return self.send_json(200, {"token": token, "email": email, "plan": row["plan"]})

    def handle_logout(self):
        auth = self.headers.get("Authorization") or ""
        if auth.startswith("Bearer "):
            conn = db()
            conn.execute("DELETE FROM sessions WHERE token=?", (auth[7:].strip(),))
            conn.commit()
            conn.close()
        return self.send_json(200, {"ok": True})

    # ---------- generation (quota gate + in-app AI) ----------
    def handle_generate(self):
        user = self.auth_user()
        if not user:
            return self.send_json(401, {"error": "not_logged_in"})
        quota = QUOTAS.get(user["plan"], 5)
        conn = db()
        used = used_today(conn, user["id"])
        if used >= quota:
            conn.close()
            return self.send_json(402, {"error": "quota_exceeded", "upgrade": True,
                                        "used_today": used, "quota": quota})
        payload = self.read_json()
        photo_url = ""
        photo_data = payload.pop("photo", "")
        if photo_data:
            try:
                path_only = save_photo(str(photo_data))
            except ValueError as e:
                conn.close()
                return self.send_json(400, {"error": str(e)})
            host = self.headers.get("Host") or "localhost"
            proto = self.headers.get("X-Forwarded-Proto") or \
                ("https" if host.endswith("onrender.com") else "http")
            photo_url = f"{proto}://{host}{path_only}"
        try:
            package = generate_package(payload, photo_url)
        except AIError as e:
            conn.close()
            return self.send_json(502, {"error": str(e)})
        if package is None:
            conn.close()
            return self.send_json(503, {"error": "ai_not_configured"})
        if str(package.get("image_url") or "").startswith("/"):
            host = self.headers.get("Host") or "localhost"
            proto = self.headers.get("X-Forwarded-Proto") or \
                ("https" if host.endswith("onrender.com") else "http")
            package["image_url"] = f"{proto}://{host}{package['image_url']}"
        # Success → count it, save it to the public showcase, then return it.
        conn.execute(
            "INSERT INTO usage(user_id, day, count) VALUES(?,?,1) "
            "ON CONFLICT(user_id, day) DO UPDATE SET count = count + 1",
            (user["id"], today()))
        conn.execute(
            "INSERT INTO showcase(title, best_title, image_url, niche, thumb_text, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (str(payload.get("title") or "")[:300], package["best_title"],
             package["image_url"], str(payload.get("niche") or "")[:60],
             str(payload.get("thumb_text") or "")[:80],
             datetime.now(timezone.utc).isoformat()))
        conn.commit()
        conn.close()
        return self.send_json(200, package)

    # ---------- Lemon Squeezy webhook ----------
    def handle_lemonsqueezy(self):
        raw = self.read_body()
        cfg = load_config()
        secret = (cfg.get("lemonsqueezy_signing_secret") or "").strip()
        verified = False
        test_mode = not secret
        if secret:
            sig = (self.headers.get("X-Signature") or "").strip()
            expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
            verified = hmac.compare_digest(expected, sig)
            if not verified:
                log_payment({"event": "webhook_rejected", "reason": "bad_signature"})
                return self.send_json(401, {"error": "bad_signature"})
        try:
            event = json.loads(raw.decode() or "{}")
        except Exception:
            log_payment({"event": "webhook_rejected", "reason": "bad_json"})
            return self.send_json(400, {"error": "bad_json"})
        meta = event.get("meta") or {}
        event_name = meta.get("event_name") or event.get("event") or "unknown"
        attrs = (event.get("data") or {}).get("attributes") or {}
        custom = meta.get("custom_data") or {}
        email = (attrs.get("user_email") or attrs.get("customer_email") or "").lower()
        entry = {"event": event_name, "email": email,
                 "custom_user_id": custom.get("user_id"),
                 "verified": verified, "test_mode": test_mode}
        if event_name in ("order_created", "subscription_created"):
            conn = db()
            row = None
            if custom.get("user_id"):
                row = conn.execute("SELECT * FROM users WHERE id=?",
                                   (custom["user_id"],)).fetchone()
            if not row and email:
                row = conn.execute("SELECT * FROM users WHERE email=?",
                                   (email,)).fetchone()
            if row:
                conn.execute("UPDATE users SET plan='pro' WHERE id=?", (row["id"],))
                conn.commit()
                entry["upgraded_user_id"] = row["id"]
            else:
                entry["upgrade"] = "no matching user"
            conn.close()
        log_payment(entry)
        note = ("TEST MODE — no signing secret configured, signature NOT verified"
                if test_mode else "signature verified")
        return self.send_json(200, {"ok": True, "note": note})

    def log_message(self, fmt, *args):
        sys.stderr.write("[thumbpack] " + fmt % args + "\n")


def main():
    init_db()
    port = int(os.environ.get("PORT") or load_config().get("port") or 7860)
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"ThumbPack app listening on http://0.0.0.0:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
