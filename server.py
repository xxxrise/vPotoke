#!/usr/bin/env python3
"""ПОТОК — Telegram-бот и сервер мини-аппа в одном файле.

Без внешних зависимостей: нужен только Python 3.9+.

Что делает:
  * бот принимает пересланные треки и отвечает кнопкой «Играть»;
  * отдаёт мини-апп (web/index.html) и сам аудиофайл по одноразовой ссылке;
  * хранит таблицу рекордов по каждому треку с проверкой подписи Telegram (initData).

Переменные окружения:
  BOT_TOKEN   токен от @BotFather (обязательно)
  PUBLIC_URL  внешний https-адрес этого сервера, например https://potok.example.ru
  PORT        порт (по умолчанию 8080), HOST — адрес (0.0.0.0)
  DATA_DIR    куда класть базу и кэш треков (./data)
  CACHE_DAYS  сколько дней хранить скачанные треки (3)
  NO_POLLING  1 — не запускать бота (для локальной проверки страницы и API)
  MAIN_APP_LINKS  1 — кнопки «Играть» открывают Main Mini App ссылкой с mode=fullscreen
              (сразу на весь экран; нужно включить Main Mini App в @BotFather, см. README)
"""
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", "8080"))
HOST = os.environ.get("HOST", "0.0.0.0")
DATA = Path(os.environ.get("DATA_DIR", "data"))
WEB = Path(os.environ.get("WEB_DIR", Path(__file__).resolve().parent / "web"))
CACHE_DAYS = float(os.environ.get("CACHE_DAYS", "3"))
NO_POLLING = os.environ.get("NO_POLLING") == "1"
MAIN_APP_LINKS = os.environ.get("MAIN_APP_LINKS") == "1"
MAX_FILE = 20 * 1024 * 1024  # лимит скачивания через обычный Bot API
AUDIO_EXT = re.compile(r"\.(mp3|m4a|aac|wav|ogg|oga|opus|flac)$", re.I)
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,48}$")
TRACK_RE = re.compile(r"^[0-9a-z-]{6,64}$")
MODES = ("easy", "normal", "hard", "expert", "vibe", "challenge")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("potok")
BOT_USERNAME = os.environ.get("BOT_USERNAME", "")

# ---------------------------------------------------------------- база
DATA.mkdir(parents=True, exist_ok=True)
(DATA / "cache").mkdir(exist_ok=True)
_db = sqlite3.connect(DATA / "potok.db", check_same_thread=False)
_db.row_factory = sqlite3.Row
_lock = threading.Lock()
with _lock:
    _db.executescript(
        """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS tracks (
          token TEXT PRIMARY KEY, user_id INTEGER, file_id TEXT, file_unique_id TEXT,
          title TEXT, artist TEXT, duration INTEGER, mime TEXT, size INTEGER, created INTEGER);
        CREATE INDEX IF NOT EXISTS tracks_user ON tracks(user_id, created);
        CREATE TABLE IF NOT EXISTS scores (
          track TEXT, mode TEXT, user_id INTEGER, name TEXT, score INTEGER, acc REAL,
          title TEXT, updated INTEGER, PRIMARY KEY (track, mode, user_id));
        CREATE INDEX IF NOT EXISTS scores_top ON scores(track, mode, score DESC);
        """
    )


def q(sql, args=(), one=False):
    with _lock:
        cur = _db.execute(sql, args)
        rows = cur.fetchall()
        _db.commit()
    return (rows[0] if rows else None) if one else rows


# ---------------------------------------------------------------- Telegram API
def tg(method, timeout=30, **params):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        res = json.load(r)
    if not res.get("ok"):
        raise RuntimeError(f"{method}: {res}")
    return res["result"]


def send(chat_id, text, **kw):
    try:
        tg("sendMessage", chat_id=chat_id, text=text, **kw)
    except Exception as e:  # noqa: BLE001
        log.warning("sendMessage failed: %s", e)


def play_url(token=None, title=None, artist=None):
    if not token:
        return PUBLIC_URL + "/"
    qs = {"track": token}
    if title:
        qs["title"] = title[:120]
    if artist:
        qs["artist"] = artist[:120]
    return PUBLIC_URL + "/?" + urllib.parse.urlencode(qs)


def play_button(token=None, title=None, artist=None, text="▶ Играть"):
    """Кнопка запуска: обычная web_app или ссылка на Main Mini App в полноэкранном режиме."""
    if MAIN_APP_LINKS and BOT_USERNAME:
        url = f"https://t.me/{BOT_USERNAME}?startapp" + (f"={token}" if token else "") + "&mode=fullscreen"
        return {"text": text, "url": url}
    return {"text": text, "web_app": {"url": play_url(token, title, artist)}}


def name_from_file(fname):
    base = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", fname or "").replace("_", " ").strip()
    parts = re.split(r"\s[-–—]\s", base, maxsplit=1)
    if len(parts) == 2:
        return parts[1].strip(), parts[0].strip()
    return base or None, None


def save_track(user_id, f, title, artist, duration, mime):
    row = q("SELECT token FROM tracks WHERE user_id=? AND file_unique_id=?", (user_id, f["file_unique_id"]), one=True)
    now = int(time.time())
    if row:
        q("UPDATE tracks SET created=?, file_id=? WHERE token=?", (now, f["file_id"], row["token"]))
        return row["token"]
    token = secrets.token_urlsafe(12)
    q(
        "INSERT INTO tracks VALUES (?,?,?,?,?,?,?,?,?,?)",
        (token, user_id, f["file_id"], f["file_unique_id"], title, artist, duration, mime, f.get("file_size"), now),
    )
    return token


def handle_update(u):
    m = u.get("message") or {}
    chat = m.get("chat") or {}
    if chat.get("type") != "private":
        return
    chat_id, user = chat["id"], m.get("from") or {}
    audio, doc, voice = m.get("audio"), m.get("document"), m.get("voice")
    f = title = artist = duration = mime = fname = None
    if audio:
        f, title, artist = audio, audio.get("title"), audio.get("performer")
        duration, mime, fname = audio.get("duration"), audio.get("mime_type"), audio.get("file_name")
    elif doc and ((doc.get("mime_type") or "").startswith("audio/") or AUDIO_EXT.search(doc.get("file_name") or "")):
        f, mime, fname = doc, doc.get("mime_type"), doc.get("file_name")
    elif voice:
        f, title, duration, mime = voice, "Голосовое", voice.get("duration"), voice.get("mime_type")

    if f:
        if (f.get("file_size") or 0) > MAX_FILE:
            send(chat_id, "Этот файл больше 20 МБ — бот не может его скачать. Пришли версию полегче, например mp3 до 20 МБ.")
            return
        if not title:
            title, a2 = name_from_file(fname)
            artist = artist or a2
        title = title or "Без названия"
        token = save_track(user.get("id"), f, title, artist, duration, mime)
        label = f"«{title}»" + (f" — {artist}" if artist else "")
        send(
            chat_id,
            f"{label} готов. Жми «Играть» — трасса подстроится под бит.",
            reply_markup={"inline_keyboard": [[play_button(token, title, artist)]]},
        )
        return

    text = (m.get("text") or "").strip()
    if text.startswith("/start"):
        send(
            chat_id,
            "Привет! Это Поток — игра, которая слышит твою музыку.\n\n"
            "Перешли мне любой трек из чата или канала — я соберу из него неоновую трассу, "
            "где всё происходит в такт. Или открой игру и начни с демо-трека.",
            reply_markup={"inline_keyboard": [[play_button(text="Открыть Поток")]]},
        )
    else:
        send(chat_id, "Перешли мне трек (аудиофайл) — и я сделаю из него уровень.")


def poll_forever():
    offset = None
    while True:
        try:
            params = {"timeout": 50, "allowed_updates": ["message"]}
            if offset is not None:
                params["offset"] = offset
            for u in tg("getUpdates", timeout=65, **params):
                offset = u["update_id"] + 1
                try:
                    handle_update(u)
                except Exception:  # noqa: BLE001
                    log.exception("update failed")
        except Exception as e:  # noqa: BLE001
            log.warning("polling error: %s", e)
            time.sleep(3)


def setup_bot():
    global BOT_USERNAME
    me = tg("getMe")
    BOT_USERNAME = me["username"]
    log.info("bot @%s", BOT_USERNAME)
    for method, params in (
        ("setMyCommands", {"commands": [{"command": "start", "description": "Открыть Поток"}]}),
        ("setChatMenuButton", {"menu_button": {"type": "web_app", "text": "Играть", "web_app": {"url": play_url()}}}),
        ("setMyDescription", {"description": "Перешли мне любой трек — и он превратится в неоновую трассу, где всё происходит в такт музыке."}),
        ("setMyShortDescription", {"short_description": "Ритм-раннер под твою музыку"}),
    ):
        try:
            tg(method, **params)
        except Exception as e:  # noqa: BLE001
            log.warning("%s failed: %s", method, e)


# ---------------------------------------------------------------- проверка initData
_secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()


def check_init_data(init_data, max_age=24 * 3600):
    """Возвращает пользователя Telegram, если подпись верна, иначе None."""
    if not init_data or not BOT_TOKEN:
        return None
    try:
        pairs = urllib.parse.parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None
    data = dict(pairs)
    got = data.pop("hash", "")
    if not got:
        return None
    variants = [data]
    if "signature" in data:  # на случай разных версий клиента проверяем оба варианта
        variants.append({k: v for k, v in data.items() if k != "signature"})
    ok = False
    for d in variants:
        dcs = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        calc = hmac.new(_secret, dcs.encode(), hashlib.sha256).hexdigest()
        if hmac.compare_digest(calc, got):
            ok = True
            break
    if not ok:
        return None
    try:
        if time.time() - int(data.get("auth_date", "0")) > max_age:
            return None
        user = json.loads(data.get("user") or "{}")
    except (ValueError, TypeError):
        return None
    return user if user.get("id") else None


def display_name(u):
    name = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x).strip()
    return (name or ("@" + u["username"] if u.get("username") else "Игрок"))[:40]


# ---------------------------------------------------------------- рекорды
def top_for(track, mode, me=None, limit=10):
    rows = q("SELECT user_id, name, score FROM scores WHERE track=? AND mode=? ORDER BY score DESC, updated ASC LIMIT ?", (track, mode, limit))
    top = [{"name": r["name"], "score": r["score"], "me": bool(me and r["user_id"] == me)} for r in rows]
    rank = None
    if me:
        mine = q("SELECT score FROM scores WHERE track=? AND mode=? AND user_id=?", (track, mode, me), one=True)
        if mine:
            rank = q("SELECT COUNT(*) AS c FROM scores WHERE track=? AND mode=? AND score>?", (track, mode, mine["score"]), one=True)["c"] + 1
    return {"top": top, "rank": rank}


# ---------------------------------------------------------------- файлы треков
_dl_lock = threading.Lock()


def cached_track(row):
    path = DATA / "cache" / re.sub(r"[^A-Za-z0-9_-]", "_", row["file_unique_id"])
    if path.exists():
        os.utime(path)
        return path
    with _dl_lock:
        if path.exists():
            return path
        info = tg("getFile", file_id=row["file_id"])
        url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{info['file_path']}"
        tmp = path.with_suffix(".part")
        with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as out:
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        os.replace(tmp, path)
    return path


def cleanup_forever():
    while True:
        cutoff = time.time() - CACHE_DAYS * 86400
        for p in (DATA / "cache").iterdir():
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass
        time.sleep(3600)


# ---------------------------------------------------------------- HTTP
_index_cache = {"mtime": None, "html": b""}


def index_html():
    path = WEB / "index.html"
    mt = path.stat().st_mtime
    if _index_cache["mtime"] != mt:
        html = path.read_text(encoding="utf-8")
        html = html.replace("<!--TG_SCRIPT-->", '<script src="https://telegram.org/js/telegram-web-app.js"></script>')
        cfg = json.dumps({"api": "", "bot": BOT_USERNAME, "server": True})
        html = html.replace("/*POTOK_CONFIG*/", "window.POTOK_CONFIG=" + cfg + ";")
        _index_cache.update(mtime=mt, html=html.encode("utf-8"))
    return _index_cache["html"]


class Handler(BaseHTTPRequestHandler):
    server_version = "Potok/1.0"

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8", {"Cache-Control": "no-store"})

    def _user(self):
        return check_init_data(self.headers.get("X-Init-Data", ""))

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        try:
            if u.path in ("/", "/index.html"):
                return self._send(200, index_html(), "text/html; charset=utf-8", {"Cache-Control": "no-cache"})
            if u.path == "/health":
                return self._json({"ok": True})
            if u.path.startswith("/track/"):
                return self._track(u.path[len("/track/"):])
            if u.path == "/api/top":
                qs = urllib.parse.parse_qs(u.query)
                track, mode = (qs.get("track") or [""])[0], (qs.get("mode") or [""])[0]
                if not TRACK_RE.match(track) or mode not in MODES:
                    return self._json({"error": "bad request"}, 400)
                user = self._user()
                return self._json(top_for(track, mode, user and user["id"]))
            if u.path == "/api/my-tracks":
                user = self._user()
                if not user:
                    return self._json({"error": "unauthorized"}, 401)
                rows = q("SELECT token, title, artist, duration FROM tracks WHERE user_id=? ORDER BY created DESC, rowid DESC LIMIT 30", (user["id"],))
                return self._json({"tracks": [dict(r) for r in rows]})
            self._json({"error": "not found"}, 404)
        except Exception:  # noqa: BLE001
            log.exception("GET %s failed", self.path)
            self._json({"error": "server error"}, 500)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            if u.path != "/api/score":
                return self._json({"error": "not found"}, 404)
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 4096:
                return self._json({"error": "bad request"}, 400)
            body = json.loads(self.rfile.read(n))
            user = self._user()
            if not user:
                return self._json({"error": "unauthorized"}, 401)
            track, mode = str(body.get("track", "")), body.get("mode")
            score = int(body.get("score", -1))
            acc = float(body.get("acc", 0))
            if not TRACK_RE.match(track) or mode not in MODES or not 0 <= score <= 5_000_000 or not 0 <= acc <= 1:
                return self._json({"error": "bad request"}, 400)
            title = str(body.get("title", ""))[:120]
            q(
                """INSERT INTO scores VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(track, mode, user_id) DO UPDATE SET
                     score=excluded.score, acc=excluded.acc, name=excluded.name, title=excluded.title, updated=excluded.updated
                   WHERE excluded.score > scores.score""",
                (track, mode, user["id"], display_name(user), score, acc, title, int(time.time())),
            )
            self._json(top_for(track, mode, user["id"]))
        except (ValueError, TypeError):
            self._json({"error": "bad request"}, 400)
        except Exception:  # noqa: BLE001
            log.exception("POST %s failed", self.path)
            self._json({"error": "server error"}, 500)

    def _track(self, token):
        if not TOKEN_RE.match(token):
            return self._json({"error": "bad token"}, 400)
        row = q("SELECT * FROM tracks WHERE token=?", (token,), one=True)
        if not row:
            return self._json({"error": "not found"}, 404)
        try:
            path = cached_track(row)
        except Exception as e:  # noqa: BLE001
            log.warning("download failed: %s", e)
            return self._json({"error": "telegram download failed"}, 502)
        size = path.stat().st_size
        ctype = row["mime"] or mimetypes.guess_type(row["title"] or "")[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "private, max-age=86400")
        self.send_header("X-Track-Title", urllib.parse.quote(row["title"] or ""))
        self.send_header("X-Track-Artist", urllib.parse.quote(row["artist"] or ""))
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(256 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)


def main():
    if not BOT_TOKEN:
        raise SystemExit("Укажи BOT_TOKEN (токен от @BotFather)")
    if not (WEB / "index.html").exists():
        raise SystemExit(f"Не найден {WEB / 'index.html'}")
    if not NO_POLLING:
        if not PUBLIC_URL.startswith("https://"):
            raise SystemExit("PUBLIC_URL должен быть https-адресом — Telegram открывает мини-аппы только по https")
        setup_bot()
        threading.Thread(target=poll_forever, daemon=True).start()
    threading.Thread(target=cleanup_forever, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    log.info("listening on %s:%s", HOST, PORT)
    srv.serve_forever()


if __name__ == "__main__":
    main()
