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
# Creem Content Moderation — every user-supplied prompt is screened BEFORE
# any generation (Creem compliance requirement for AI generation products).
# Contract (https://docs.creem.io/features/moderation):
#   POST {base}/v1/moderation/prompt
#   base = https://api.creem.io (prod keys "creem_...") or
#          https://test-api.creem.io (sandbox keys "creem_test_...")
#   headers: x-api-key: <CREEM_API_KEY>, content-type: application/json
#   body:    {"prompt": str, "external_id": str (optional)}
#   reply:   {"id", "object": "moderation_result", "prompt", "external_id",
#             "decision": "allow" | "flag" | "deny", "usage": {"units": n}}
# "flag" is treated exactly like "deny" (blocked), per Creem's own
# recommendation. When CREEM_API_KEY is set but the API call fails, we fail
# CLOSED (503, no generation). When the key is unset (local dev), we log a
# warning and skip screening.
# ---------------------------------------------------------------------------

SUPPORT_EMAIL = "reply2falcon@gmail.com"


def moderation_block(text, external_id=""):
    """Screen user text via the Creem Moderation API. Returns None when the
    text may proceed to generation, else (http_status, error_dict)."""
    text = (text or "").strip()
    if not text:
        return None
    key = (os.environ.get("CREEM_API_KEY") or "").strip()
    if not key:
        print("[thumbpack] WARNING: CREEM_API_KEY not set — content "
              "moderation SKIPPED (dev mode)", file=sys.stderr, flush=True)
        return None
    base = ("https://test-api.creem.io" if key.startswith("creem_test_")
            else "https://api.creem.io")
    body = {"prompt": text}
    if external_id:
        body["external_id"] = external_id
    req = urllib.request.Request(
        base + "/v1/moderation/prompt",
        data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "x-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
    except Exception as e:  # fail closed — never generate unscreened
        print(f"[thumbpack] moderation API call failed: {e}",
              file=sys.stderr, flush=True)
        return (503, {"error": "moderation_unavailable",
                      "message": "Content moderation is temporarily "
                                 "unavailable. Please try again shortly."})
    decision = str(data.get("decision") or "").lower()
    if decision == "allow":
        return None
    if decision == "deny":
        return (400, {"error": "prompt_rejected",
                      "message": "Your prompt was rejected because it "
                                 "violates our content policy (see "
                                 "/acceptable-use). Please revise and try "
                                 "again."})
    if decision == "flag":
        return (400, {"error": "prompt_flagged",
                      "message": "Your prompt could not be processed "
                                 "because it was flagged by content "
                                 "moderation. Please revise and try again."})
    # Unknown verdict — fail closed.
    print(f"[thumbpack] moderation returned unknown decision: {data!r}",
          file=sys.stderr, flush=True)
    return (503, {"error": "moderation_unavailable",
                  "message": "Content moderation is temporarily "
                             "unavailable. Please try again shortly."})


# ---------------------------------------------------------------------------
# Legal pages (Creem compliance: Privacy Policy, Terms of Service,
# Acceptable Use Policy incl. explicit NSFW rules). Inline HTML in the same
# dark style as index.html. English-only is acceptable for compliance.
# ---------------------------------------------------------------------------

def _legal_page(title, body_html):
    cfg = load_config()
    operator = (cfg.get("operator_legal_name") or "").strip()
    operator_html = (f"<p>Operated by {operator}.</p>" if operator else "")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} — ThumbPack</title>
<style>
  body{{margin:0;background:#0b0e14;color:#e8ecf4;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif}}
  .wrap{{max-width:820px;margin:0 auto;padding:32px 18px 60px;line-height:1.65}}
  h1{{font-size:30px}} h2{{font-size:19px;margin-top:30px;color:#ffb020}}
  a{{color:#57c7ff}} .muted{{color:#9aa5bb;font-size:13px}}
  footer{{margin-top:44px;border-top:1px solid #2a3247;padding-top:18px;
    color:#9aa5bb;font-size:14px;line-height:1.7}}
  footer a{{margin-right:16px}}
</style>
</head>
<body>
<div class="wrap">
<h1>{title}</h1>
<p class="muted">ThumbPack — Thumbnail Packaging Suite · Last updated: October 23, 2026</p>
{body_html}
<footer>
  <a href="/">Home</a><a href="/privacy">Privacy Policy</a><a href="/terms">Terms of Service</a><a href="/acceptable-use">Acceptable Use Policy</a>
  <p>Support: <a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a></p>
  {operator_html}
</footer>
</div>
</body>
</html>"""


PRIVACY_BODY = """
<h2>What we collect</h2>
<p>We collect the minimum needed to run ThumbPack: your account email
address, a securely hashed password (we never store your plain password),
your plan status, and a per-day count of how many generations you have
used.</p>
<h2>Photos you upload</h2>
<p>If you use photo mode, the photo you upload is used <b>only</b> to
generate your thumbnail package. Uploaded photos and generated images are
stored on our server under <code>/uploads/</code> so the finished thumbnail
can be delivered back to you (and shown in the public “latest thumbnails”
showcase together with the title/tags generated for it). We do not sell
photos, and we do not use them to train models. You may ask us to delete
your uploaded photos at any time by emailing
<a href="mailto:reply2falcon@gmail.com">reply2falcon@gmail.com</a> from your
account email address; we will delete them on request.</p>
<h2>Payments</h2>
<p>Subscriptions are sold through third-party payment processors
(Gumroad, Creem and Lemon Squeezy). Your card details go directly to the
processor — we never receive, see or store your full card number on our
servers. The processors send us a payment notification containing your
email address so we can activate (or, after a refund/cancellation,
deactivate) your Pro plan.</p>
<h2>Content moderation</h2>
<p>Prompts you submit are screened by the Creem Content Moderation API
before generation (see our <a href="/acceptable-use">Acceptable Use
Policy</a>). The prompt text is sent to Creem for that screening only.</p>
<h2>Cookies &amp; analytics</h2>
<p>We keep this minimal: your login session is kept in your browser's
local storage (not a tracking cookie), and we do not run third-party
advertising or cross-site analytics trackers.</p>
<h2>Data retention &amp; your rights</h2>
<p>Account data is kept while your account exists. You may request access
to, correction of, or deletion of your account and uploaded photos at any
time via the support email below.</p>
<h2>Contact</h2>
<p>Privacy questions: <a href="mailto:reply2falcon@gmail.com">reply2falcon@gmail.com</a></p>
"""

TERMS_BODY = """
<h2>The service</h2>
<p>ThumbPack is an online tool that generates video thumbnail packages —
title suggestions with predicted CTR scores, SEO tags, and an AI-generated
thumbnail image (or a package built around a photo you upload). Output is
generated by third-party AI models and is provided “as is” without any
guarantee of views, clicks or revenue.</p>
<h2>Plans and pricing</h2>
<p><b>Free plan:</b> 5 generations per day, no payment required.
<b>ThumbPack Pro: $9/month</b> (USD), 200 generations per day. The Pro
price is shown on this site before you sign up or check out. You may
cancel your subscription at any time through the payment processor you
paid with; cancellation stops future renewals. If a payment is refunded,
charged back or a subscription is cancelled/ended, the account returns to
the Free plan automatically.</p>
<h2>Your content and rights</h2>
<p>You are responsible for the prompts and photos you submit. Only upload
photos and request content that you own or have the legal right to use
(including rights in any person's likeness). As between you and us, you
retain your rights in content you create with ThumbPack, subject to the
terms of the underlying AI model providers. You must follow our
<a href="/acceptable-use">Acceptable Use Policy</a> at all times;
violations may lead to blocked generations and account termination.</p>
<h2>Accounts</h2>
<p>You are responsible for activity under your account and for keeping
your password confidential. Accounts are for one person's use.</p>
<h2>Availability</h2>
<p>Generation depends on third-party AI providers and free-tier quotas;
we may limit, suspend or change the service, and generation may
occasionally fail or be delayed. Daily quotas reset at midnight UTC.</p>
<h2>Limitation of liability</h2>
<p>To the maximum extent permitted by law, ThumbPack and its operator are
not liable for indirect, incidental or consequential losses (including
lost views, revenue or profits) arising from use of the service or from
AI-generated output. Nothing in these terms limits liability that cannot
lawfully be limited.</p>
<h2>Contact</h2>
<p>Questions about these terms:
<a href="mailto:reply2falcon@gmail.com">reply2falcon@gmail.com</a></p>
"""

ACCEPTABLE_USE_BODY = """
<h2>Purpose</h2>
<p>ThumbPack generates images and text with AI. To keep the service safe
and available to everyone, all prompts are screened by automated content
moderation (the Creem Content Moderation API) before generation, and the
rules below apply to every prompt, upload and generation.</p>
<h2>Prohibited content — including explicit NSFW rules</h2>
<p>You must NOT use ThumbPack to request, generate, upload or distribute:</p>
<ul>
<li><b>Sexual or NSFW content of any kind</b> — nudity, sexual acts,
erotic or sexually suggestive content, fetish content, or “borderline”
content designed to evade this rule. ThumbPack is a strictly
family-safe thumbnail tool: NSFW prompts are blocked, without
exception.</li>
<li><b>Any content involving minors</b> — including sexualised, exploitative
or otherwise harmful depictions of people under 18, whether real,
photorealistic or illustrated. Such attempts are blocked and reported
where required by law.</li>
<li><b>Violence and gore</b> — graphic violence, gore, or content that
glorifies terrorism, extremism or self-harm.</li>
<li><b>Hate and harassment</b> — content attacking or demeaning people
based on protected characteristics, or targeted harassment/bullying of
identifiable individuals.</li>
<li><b>Impersonation and deepfakes</b> — content that impersonates real
people, or photorealistic depictions of real, identifiable people
(including public figures) presented as genuine, or intended to deceive
viewers.</li>
<li><b>Illegal content</b> — anything that violates applicable law,
infringes intellectual-property or privacy rights, promotes illegal
goods/services, fraud, scams or other deceptive practices.</li>
</ul>
<h2>How enforcement works</h2>
<p>Every prompt is screened automatically before it reaches an AI model.
Prompts that the moderation system denies or flags are rejected and no
generation takes place. Repeated or serious violations may lead to
immediate account termination without refund eligibility beyond what the
payment processor's own policies provide. Moderation decisions are made
by the Creem Content Moderation API against Creem's content policies, in
addition to the rules above.</p>
<h2>Appeals and contact</h2>
<p>If you believe a generation was blocked in error, contact
<a href="mailto:reply2falcon@gmail.com">reply2falcon@gmail.com</a> with your
account email and a description of the prompt.</p>
"""



# ---------------------------------------------------------------------------
# Generation — direct Gemini call + strict-JSON parse.
# Prompt text and response schema are verbatim ports of the n8n workflow's
# "Prepare" / "Parse Package" Code nodes (../build-workflow.py).
# ---------------------------------------------------------------------------

class AIError(Exception):
    """Generation backend failure → HTTP 502 (after retries)."""


class ModerationBlocked(Exception):
    """Creem moderation rejected a prompt → carries (http_status, body)."""

    def __init__(self, blocked):
        self.code, self.body = blocked
        super().__init__(self.body.get("error", "prompt_rejected"))


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
        # The model-written image prompt is also screened before it is
        # routed to any image generation model (Creem requirement).
        blocked = moderation_block(image_prompt)
        if blocked:
            raise ModerationBlocked(blocked)
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
    def send_html(self, code, html):
        body = html.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            return self.serve_file("index.html", "text/html; charset=utf-8")
        if path == "/privacy":
            return self.send_html(200, _legal_page("Privacy Policy", PRIVACY_BODY))
        if path == "/terms":
            return self.send_html(200, _legal_page("Terms of Service", TERMS_BODY))
        if path == "/acceptable-use":
            return self.send_html(200, _legal_page("Acceptable Use Policy", ACCEPTABLE_USE_BODY))
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
                "checkout_url_pro": cfg.get("lemonsqueezy_checkout_url_pro", ""),
                "gumroad_checkout_url": cfg.get("gumroad_checkout_url", ""),
                "creem_checkout_url": cfg.get("creem_checkout_url", ""),
                "operator_legal_name": cfg.get("operator_legal_name", "")})
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
            if path == "/webhook/gumroad":
                return self.handle_gumroad()
            if path == "/webhook/creem":
                return self.handle_creem()
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
        payload = self.read_json()
        # Creem Content Moderation: screen the user-supplied prompt text
        # (title + thumbnail text) BEFORE quota, billing or any model call.
        mod_text = " ".join(
            str(payload.get(k) or "").strip()
            for k in ("title", "thumb_text")).strip()
        blocked = moderation_block(mod_text, f"user_{user['id']}")
        if blocked:
            code, obj = blocked
            return self.send_json(code, obj)
        quota = QUOTAS.get(user["plan"], 5)
        conn = db()
        used = used_today(conn, user["id"])
        if used >= quota:
            conn.close()
            return self.send_json(402, {"error": "quota_exceeded", "upgrade": True,
                                        "used_today": used, "quota": quota})
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
        except ModerationBlocked as e:
            conn.close()
            return self.send_json(e.code, e.body)
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

    # ---------- shared plan switching (Gumroad / Creem) ----------
    def set_plan_by_email(self, email, plan, source, entry):
        email = (email or "").strip().lower()
        if not email:
            entry["upgrade"] = "no buyer email in event"
            return
        conn = db()
        row = conn.execute("SELECT * FROM users WHERE email=?",
                           (email,)).fetchone()
        if row:
            conn.execute("UPDATE users SET plan=? WHERE id=?",
                         (plan, row["id"]))
            conn.commit()
            entry["user_id"] = row["id"]
            entry["plan_set"] = plan
            entry["source"] = source
        else:
            entry["upgrade"] = "no matching user"
        conn.close()

    # ---------- Gumroad Ping webhook ----------
    def handle_gumroad(self):
        # Gumroad pings are unsigned; the Ping URL carries our own secret
        # (?secret=...) and GUMROAD_PING_SECRET must be set in the env.
        secret = (os.environ.get("GUMROAD_PING_SECRET") or "").strip()
        if not secret:
            return self.send_json(503, {"error": "gumroad_not_configured"})
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query)
        given = (query.get("secret") or [""])[0]
        if not hmac.compare_digest(given, secret):
            log_payment({"event": "gumroad_rejected", "reason": "bad_secret"})
            return self.send_json(401, {"error": "bad_secret"})
        form = urllib.parse.parse_qs(self.read_body().decode("utf-8", "replace"))
        f = {k: (v[0] if v else "") for k, v in form.items()}
        resource = (f.get("resource_name") or "sale").strip()
        entry = {"event": "gumroad_" + resource, "email": f.get("email", ""),
                 "sale_id": f.get("sale_id", ""),
                 "product": f.get("permalink", "")}
        cfg = load_config()
        want = (cfg.get("gumroad_product_permalink") or "").strip()
        if want and entry["product"] and entry["product"] != want:
            entry["ignored"] = "different product"
        elif resource == "sale":
            self.set_plan_by_email(f.get("email", ""), "pro", "gumroad", entry)
        elif resource in ("refund", "dispute", "cancellation",
                          "subscription_ended"):
            self.set_plan_by_email(f.get("email", ""), "free", "gumroad", entry)
        log_payment(entry)
        return self.send_json(200, {"ok": True})

    # ---------- Creem webhook ----------
    def handle_creem(self):
        raw = self.read_body()
        secret = (os.environ.get("CREEM_WEBHOOK_SECRET") or "").strip()
        if not secret:
            return self.send_json(503, {"error": "creem_not_configured"})
        sig = (self.headers.get("creem-signature") or "").strip()
        expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            log_payment({"event": "creem_rejected", "reason": "bad_signature"})
            return self.send_json(401, {"error": "bad_signature"})
        try:
            event = json.loads(raw.decode() or "{}")
        except Exception:
            log_payment({"event": "creem_rejected", "reason": "bad_json"})
            return self.send_json(400, {"error": "bad_json"})
        etype = event.get("eventType") or event.get("event") or "unknown"
        obj = event.get("object") or {}
        customer = obj.get("customer") or {}
        email = (customer.get("email") or obj.get("email") or "")
        entry = {"event": "creem_" + str(etype), "email": email}
        cfg = load_config()
        want = (cfg.get("creem_product_id") or "").strip()
        prod = obj.get("product") or {}
        pid = prod.get("id") if isinstance(prod, dict) else ""
        if want and pid and pid != want:
            entry["ignored"] = "different product"
        elif etype in ("checkout.completed", "subscription.active",
                       "subscription.paid"):
            self.set_plan_by_email(email, "pro", "creem", entry)
        elif etype in ("subscription.canceled", "subscription.expired",
                       "subscription.paused", "refund.created",
                       "dispute.created"):
            self.set_plan_by_email(email, "free", "creem", entry)
        log_payment(entry)
        return self.send_json(200, {"ok": True})

    # ---------- Lemon Squeezy webhook ----------
    def handle_lemonsqueezy(self):
        raw = self.read_body()
        cfg = load_config()
        secret = ((os.environ.get("LEMONSQUEEZY_SIGNING_SECRET") or "").strip()
                  or (cfg.get("lemonsqueezy_signing_secret") or "").strip())
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
