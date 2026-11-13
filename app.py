"""
Clinic Booking API  (Flask + a database)

Run on your own computer:
    export API_KEY="your-secret-key"      (Windows PowerShell:  $env:API_KEY="your-secret-key")
    python app.py

On a real host (Render) the app is started by gunicorn and DATABASE_URL points
to a Postgres database, so bookings survive restarts. With no DATABASE_URL it
falls back to a local SQLite file (bookings.db), which is perfect for testing.
"""
import base64
import hashlib
import hmac
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

from flask import Flask, Response, request
from werkzeug.exceptions import HTTPException

# --------------------------------------------------------------------------
# Settings (read from environment variables, so no secrets live in the code)
# --------------------------------------------------------------------------
API_KEY = os.environ.get("API_KEY", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
SQLITE_PATH = os.environ.get("SQLITE_PATH", "bookings.db")
USE_PG = bool(DATABASE_URL)

RATE_LIMIT = 5            # attempts allowed ...
RATE_WINDOW = 60          # ... per this many seconds, per email

if not API_KEY:
    sys.exit("ERROR: set the API_KEY environment variable before starting the server.")

if USE_PG:
    import psycopg2  # only needed when a Postgres database is used
    INTEGRITY_ERRORS = (sqlite3.IntegrityError, psycopg2.IntegrityError)
else:
    INTEGRITY_ERRORS = (sqlite3.IntegrityError,)

app = Flask(__name__)
app.url_map.strict_slashes = False


# --------------------------------------------------------------------------
# Database helper: one "Tx" = one transaction (all-or-nothing group of changes)
# --------------------------------------------------------------------------
class Tx:
    def __enter__(self):
        if USE_PG:
            self.conn = psycopg2.connect(DATABASE_URL)
        else:
            self.conn = sqlite3.connect(SQLITE_PATH, timeout=30, isolation_level=None)
            self.conn.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if USE_PG:
                (self.conn.rollback if exc_type else self.conn.commit)()
            else:
                self.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.conn.close()
        return False

    def run(self, sql, params=()):
        cur = self.conn.cursor()
        cur.execute(sql.replace("?", "%s") if USE_PG else sql, params)
        return cur

    def all(self, sql, params=()):
        cur = self.run(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def one(self, sql, params=()):
        rows = self.all(sql, params)
        return rows[0] if rows else None


def init_db():
    auto_id = "BIGSERIAL PRIMARY KEY" if USE_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    with Tx() as tx:
        tx.run("""CREATE TABLE IF NOT EXISTS slots (
                    id TEXT PRIMARY KEY, provider TEXT NOT NULL,
                    start_text TEXT NOT NULL, end_text TEXT NOT NULL,
                    start_ms BIGINT NOT NULL)""")
        tx.run(f"""CREATE TABLE IF NOT EXISTS bookings (
                    seq {auto_id}, id TEXT UNIQUE NOT NULL, slot_id TEXT NOT NULL,
                    name TEXT NOT NULL, email TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL)""")
        # The database itself refuses a 2nd confirmed booking for one slot.
        # This is what makes "two people at the exact same moment" safe.
        tx.run("""CREATE UNIQUE INDEX IF NOT EXISTS one_confirmed_per_slot
                  ON bookings (slot_id) WHERE status = 'confirmed'""")
        tx.run("CREATE INDEX IF NOT EXISTS bookings_by_email ON bookings (email, seq)")
        tx.run("""CREATE TABLE IF NOT EXISTS idempotency (
                    key TEXT PRIMARY KEY, body_hash TEXT NOT NULL,
                    status INTEGER NOT NULL, response TEXT NOT NULL)""")


init_db()


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def reply(status, payload, headers=None):
    return Response(json.dumps(payload), status=status,
                    mimetype="application/json", headers=headers or {})


def fail(status, message, headers=None):
    return reply(status, {"error": message}, headers)


def parse_time_ms(text):
    """'2026-12-10T09:00:00Z' -> milliseconds since 1970, or None if invalid."""
    if not isinstance(text, str):
        return None
    try:
        dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def now_ms():
    return int(time.time() * 1000)


def public_booking(row):
    return {k: row[k] for k in ("id", "slot_id", "name", "email", "status", "created_at")}


EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

# Rate limit memory: email -> list of attempt times (seconds)
_attempts = {}
_attempts_lock = threading.Lock()


def rate_limited(email):
    """Record an attempt. Returns seconds to wait if the limit is exceeded, else None."""
    now = time.time()
    with _attempts_lock:
        recent = [t for t in _attempts.get(email, []) if now - t < RATE_WINDOW]
        if len(recent) >= RATE_LIMIT:
            _attempts[email] = recent
            return max(1, math.ceil(recent[0] + RATE_WINDOW - now))  # whole seconds, rounded up
        recent.append(now)
        _attempts[email] = recent
        return None


# --------------------------------------------------------------------------
# Level 2: every API request needs the right X-API-Key
# (the web page itself, GET /, is the only thing that can be opened without it)
# --------------------------------------------------------------------------
@app.before_request
def check_key():
    if request.method == "GET" and request.path in ("/", "/index.html"):
        return None
    sent = request.headers.get("X-API-Key", "")
    if not hmac.compare_digest(sent.encode(), API_KEY.encode()):
        return fail(401, "missing or invalid API key")
    return None


@app.errorhandler(HTTPException)
def http_error(e):
    return fail(e.code or 500, e.name.lower())


@app.errorhandler(Exception)
def crash(e):
    app.logger.exception("unexpected error")
    return fail(500, "internal error")


def read_json_body():
    raw = request.get_data()
    if not raw.strip():
        return {}, None
    try:
        return json.loads(raw), None
    except ValueError:
        return None, fail(400, "invalid JSON body")


# --------------------------------------------------------------------------
# The web page
# --------------------------------------------------------------------------
HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "index.html"), encoding="utf-8") as f:
    PAGE = f.read().replace("__API_KEY__", json.dumps(API_KEY))


@app.get("/")
@app.get("/index.html")
def home():
    return Response(PAGE, mimetype="text/html")


# --------------------------------------------------------------------------
# Level 1: POST /reset, GET /slots
# --------------------------------------------------------------------------
@app.post("/reset")
def reset():
    body, bad = read_json_body()
    if bad:
        return bad
    if not isinstance(body, dict) or not isinstance(body.get("slots"), list):
        return fail(400, "slots must be a list")
    clean = {}
    for s in body["slots"]:
        if not isinstance(s, dict) or not isinstance(s.get("id"), str) or not s["id"].strip() \
                or not isinstance(s.get("provider"), str):
            return fail(400, "each slot needs id, provider, start and end")
        start_ms, end_ms = parse_time_ms(s.get("start")), parse_time_ms(s.get("end"))
        if start_ms is None or end_ms is None:
            return fail(400, "invalid start or end time")
        clean[s["id"]] = (s["id"], s["provider"], s["start"], s["end"], start_ms)
    with Tx() as tx:
        tx.run("DELETE FROM bookings")
        tx.run("DELETE FROM idempotency")
        tx.run("DELETE FROM slots")
        for row in clean.values():
            tx.run("INSERT INTO slots (id, provider, start_text, end_text, start_ms) "
                   "VALUES (?,?,?,?,?)", row)
    with _attempts_lock:
        _attempts.clear()
    return reply(200, {"ok": True})


@app.get("/slots")
def list_slots():
    provider = request.args.get("provider")
    sql = ("SELECT s.id, s.provider, s.start_text, s.end_text, "
           "NOT EXISTS (SELECT 1 FROM bookings b WHERE b.slot_id = s.id "
           "AND b.status = 'confirmed') AS available FROM slots s ")
    params = ()
    if provider is not None:
        sql += "WHERE s.provider = ? "
        params = (provider,)
    sql += "ORDER BY s.start_ms, s.id"
    with Tx() as tx:
        rows = tx.all(sql, params)
    return reply(200, {"slots": [
        {"id": r["id"], "provider": r["provider"], "start": r["start_text"],
         "end": r["end_text"], "available": bool(r["available"])} for r in rows]})


# --------------------------------------------------------------------------
# POST /bookings  (levels 1-5)
# --------------------------------------------------------------------------
def book_inside_transaction(tx, body):
    """Validate and create the booking. Returns (status_code, payload)."""
    slot_id = body.get("slot_id")
    name = body.get("name")
    email = body.get("email")
    if not isinstance(slot_id, str) or not slot_id.strip():
        return 400, {"error": "slot_id is required"}
    if not isinstance(name, str) or not name.strip():
        return 400, {"error": "name is required"}
    if not isinstance(email, str) or not email.strip():
        return 400, {"error": "email is required"}
    name = name.strip()
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        return 400, {"error": "email is not valid"}

    slot = tx.one("SELECT id, start_ms FROM slots WHERE id = ?", (slot_id,))
    if not slot:
        return 404, {"error": "slot not found"}
    if slot["start_ms"] <= now_ms():
        return 400, {"error": "slot has already started"}
    if tx.one("SELECT 1 AS x FROM bookings WHERE slot_id = ? AND status = 'confirmed'",
              (slot_id,)):
        return 409, {"error": "slot already booked"}

    created = datetime.now(timezone.utc)
    booking = {"id": str(uuid.uuid4()), "slot_id": slot_id, "name": name, "email": email,
               "status": "confirmed",
               "created_at": created.strftime("%Y-%m-%dT%H:%M:%S.")
               + f"{created.microsecond // 1000:03d}Z"}
    tx.run("INSERT INTO bookings (id, slot_id, name, email, status, created_at) "
           "VALUES (?,?,?,?,?,?)",
           (booking["id"], slot_id, name, email, "confirmed", booking["created_at"]))
    return 201, booking


def stored_response(status, payload):
    return Response(payload if isinstance(payload, str) else json.dumps(payload),
                    status=status, mimetype="application/json")


@app.post("/bookings")
def create_booking():
    body, bad = read_json_body()
    if bad:
        return bad
    if not isinstance(body, dict):
        return fail(400, "body must be a JSON object")

    idem_key = request.headers.get("Idempotency-Key") or None
    body_hash = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()

    # Level 4: have we already seen this Idempotency-Key?
    if idem_key:
        with Tx() as tx:
            seen = tx.one("SELECT body_hash, status, response FROM idempotency WHERE key = ?",
                          (idem_key,))
        if seen:
            if seen["body_hash"] != body_hash:
                return fail(422, "Idempotency-Key was already used with a different body")
            return stored_response(seen["status"], seen["response"])

    # Level 5: at most 5 attempts per email per minute (failed attempts count too)
    email = body.get("email")
    if isinstance(email, str) and email.strip():
        wait = rate_limited(email.strip().lower())
        if wait is not None:
            return fail(429, "too many attempts, please wait", {"Retry-After": str(wait)})

    try:
        with Tx() as tx:
            if idem_key:
                claimed = tx.run(
                    "INSERT INTO idempotency (key, body_hash, status, response) "
                    "VALUES (?,?,0,'') ON CONFLICT (key) DO NOTHING",
                    (idem_key, body_hash)).rowcount
                if claimed == 0:  # another request with this key got there first
                    seen = tx.one("SELECT body_hash, status, response FROM idempotency "
                                  "WHERE key = ?", (idem_key,))
                    if seen["body_hash"] != body_hash:
                        return fail(422, "Idempotency-Key was already used with a different body")
                    return stored_response(seen["status"], seen["response"])
            status, payload = book_inside_transaction(tx, body)
            if idem_key:
                tx.run("UPDATE idempotency SET status = ?, response = ? WHERE key = ?",
                       (status, json.dumps(payload), idem_key))
    except INTEGRITY_ERRORS:
        # Two requests grabbed the same slot at the same instant; the database
        # let only one through. This one lost.
        status, payload = 409, {"error": "slot already booked"}
        if idem_key:
            with Tx() as tx:
                tx.run("INSERT INTO idempotency (key, body_hash, status, response) "
                       "VALUES (?,?,?,?) ON CONFLICT (key) DO NOTHING",
                       (idem_key, body_hash, status, json.dumps(payload)))
    return reply(status, payload)


# --------------------------------------------------------------------------
# GET /bookings (a person's bookings, paged) and GET/DELETE /bookings/<id>
# --------------------------------------------------------------------------
@app.get("/bookings")
def list_bookings():
    email = request.args.get("email")
    if email is None or not email.strip():
        return fail(400, "email is required")
    email = email.strip().lower()

    limit = 10
    raw_limit = request.args.get("limit")
    if raw_limit is not None:
        if not re.fullmatch(r"\d+", raw_limit.strip()) or not 1 <= int(raw_limit) <= 50:
            return fail(400, "limit must be a whole number from 1 to 50")
        limit = int(raw_limit)

    after = 0
    cursor = request.args.get("cursor")
    if cursor:
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            after = int(base64.urlsafe_b64decode(padded).decode())
            if after < 0:
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            return fail(400, "invalid cursor")

    with Tx() as tx:
        rows = tx.all("SELECT seq, id, slot_id, name, email, status, created_at FROM bookings "
                      "WHERE email = ? AND seq > ? ORDER BY seq LIMIT ?",
                      (email, after, limit + 1))
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        next_cursor = base64.urlsafe_b64encode(str(page[-1]["seq"]).encode()).decode().rstrip("=")
    return reply(200, {"bookings": [public_booking(r) for r in page],
                       "next_cursor": next_cursor})


@app.get("/bookings/<booking_id>")
def get_booking(booking_id):
    with Tx() as tx:
        row = tx.one("SELECT id, slot_id, name, email, status, created_at FROM bookings "
                     "WHERE id = ?", (booking_id,))
    if not row:
        return fail(404, "booking not found")
    return reply(200, public_booking(row))


@app.delete("/bookings/<booking_id>")
def cancel_booking(booking_id):
    with Tx() as tx:
        changed = tx.run("UPDATE bookings SET status = 'cancelled' "
                         "WHERE id = ? AND status = 'confirmed'", (booking_id,)).rowcount
        row = tx.one("SELECT id, slot_id, name, email, status, created_at FROM bookings "
                     "WHERE id = ?", (booking_id,))
    if not row:
        return fail(404, "booking not found")
    if changed == 0:
        return fail(409, "booking already cancelled")
    return reply(200, public_booking(row))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), threaded=True)
