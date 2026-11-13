"""
Self-check for your Clinic Booking API. Uses only built-in Python (no installs).

    python test_api.py http://localhost:5000 your-secret-key
    python test_api.py https://your-app.onrender.com your-secret-key

WARNING: this calls /reset, which erases all slots and bookings. Run it BEFORE
you submit, never after.
"""
import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = sys.argv[1].rstrip("/")
KEY = sys.argv[2]
passed = failed = 0


def call(method, path, body=None, key=KEY, headers=None):
    h = {"Content-Type": "application/json"}
    if key is not None:
        h["X-API-Key"] = key
    h.update(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"null"), r.headers
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw), e.headers
        except ValueError:
            return e.code, raw, e.headers


def check(name, condition):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + name)
    passed += condition
    failed += not condition


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


now = datetime.now(timezone.utc)
fut = now + timedelta(days=30)
slots = [
    {"id": "s1", "provider": "Dr Ade", "start": iso(fut), "end": iso(fut + timedelta(minutes=30))},
    {"id": "s2", "provider": "Dr Ade", "start": iso(fut - timedelta(days=1)), "end": iso(fut)},
    {"id": "s3", "provider": "Dr Bola", "start": iso(fut + timedelta(days=1)), "end": iso(fut + timedelta(days=2))},
    {"id": "s4", "provider": "Dr Bola", "start": iso(fut + timedelta(days=2)), "end": iso(fut + timedelta(days=3))},
    {"id": "s5", "provider": "Dr Bola", "start": iso(fut + timedelta(days=3)), "end": iso(fut + timedelta(days=4))},
    {"id": "past", "provider": "Dr Ade", "start": iso(now - timedelta(days=1)), "end": iso(now - timedelta(hours=23))},
]

print("Level 2: API key")
check("no key -> 401", call("GET", "/slots", key=None)[0] == 401)
check("wrong key -> 401", call("GET", "/slots", key="nope")[0] == 401)

print("Level 1: reset, list, book, look up")
check("reset -> 200", call("POST", "/reset", {"slots": slots})[0] == 200)
st, data, _ = call("GET", "/slots")
check("slots earliest first", st == 200 and [s["id"] for s in data["slots"]][:2] == ["past", "s2"])
check("all slots available", all(s["available"] for s in data["slots"]))
check("provider filter", [s["id"] for s in call("GET", "/slots?provider=Dr%20Bola")[1]["slots"]] == ["s3", "s4", "s5"])
st, b, _ = call("POST", "/bookings", {"slot_id": "s1", "name": "  Ada Obi ", "email": " ADA@Example.com "})
check("book -> 201 confirmed", st == 201 and b["status"] == "confirmed")
check("name trimmed, email lower-cased", b["name"] == "Ada Obi" and b["email"] == "ada@example.com")
check("get booking by id", call("GET", "/bookings/" + b["id"])[1] == b)
check("unknown booking -> 404", call("GET", "/bookings/nope")[0] == 404)
check("booked slot not available", [s for s in call("GET", "/slots")[1]["slots"] if s["id"] == "s1"][0]["available"] is False)

print("Level 2: validation")
good = {"slot_id": "s3", "name": "Bo", "email": "bo@example.com"}
for field in ("slot_id", "name", "email"):
    bad = dict(good); bad[field] = "  "
    check(f"empty {field} -> 400", call("POST", "/bookings", bad)[0] == 400)
    bad = dict(good); del bad[field]
    check(f"missing {field} -> 400", call("POST", "/bookings", bad)[0] == 400)
check("bad email -> 400", call("POST", "/bookings", {**good, "email": "not-an-email"})[0] == 400)
# (different emails here so we don't trip the level 5 limit by accident)
check("unknown slot -> 404", call("POST", "/bookings", {**good, "slot_id": "zzz", "email": "u1@example.com"})[0] == 404)
check("slot in the past -> 400", call("POST", "/bookings", {**good, "slot_id": "past", "email": "u2@example.com"})[0] == 400)

print("Level 3: double booking and cancel")
check("already booked -> 409", call("POST", "/bookings", {"slot_id": "s1", "name": "X", "email": "x@example.com"})[0] == 409)
call("POST", "/reset", {"slots": slots})
results = []
def grab(i):
    results.append(call("POST", "/bookings", {"slot_id": "s4", "name": f"P{i}", "email": f"p{i}@example.com"})[0])
threads = [threading.Thread(target=grab, args=(i,)) for i in range(4)]
[t.start() for t in threads]; [t.join() for t in threads]
check("4 simultaneous requests -> exactly one 201", results.count(201) == 1 and results.count(409) == 3)
st, b, _ = call("POST", "/bookings", {"slot_id": "s5", "name": "Cy", "email": "cy@example.com"})
st1, c, _ = call("DELETE", "/bookings/" + b["id"])
check("cancel -> 200 cancelled", st1 == 200 and c["status"] == "cancelled")
check("cancel again -> 409", call("DELETE", "/bookings/" + b["id"])[0] == 409)
check("cancel unknown -> 404", call("DELETE", "/bookings/nope")[0] == 404)
check("slot free again", call("POST", "/bookings", {"slot_id": "s5", "name": "Di", "email": "di@example.com"})[0] == 201)

print("Level 4: idempotency and paging")
call("POST", "/reset", {"slots": slots})
body = {"slot_id": "s1", "name": "Eve", "email": "eve@example.com"}
h = {"Idempotency-Key": "key-1"}
s1, r1, _ = call("POST", "/bookings", body, headers=h)
s2, r2, _ = call("POST", "/bookings", body, headers=h)
check("same key+body -> same status and id", s1 == s2 == 201 and r1["id"] == r2["id"])
check("different body, same key -> 422", call("POST", "/bookings", {**body, "name": "Other"}, headers=h)[0] == 422)
check("no duplicate was created", len(call("GET", "/bookings?email=EVE@example.com")[1]["bookings"]) == 1)
for sid in ("s2", "s3", "s4"):
    call("POST", "/bookings", {"slot_id": sid, "name": "Eve", "email": "eve@example.com"})
seen, cursor, pages = [], None, 0
while True:
    st, page, _ = call("GET", "/bookings?email=eve@example.com&limit=3" + (f"&cursor={cursor}" if cursor else ""))
    seen += [x["slot_id"] for x in page["bookings"]]; pages += 1
    cursor = page["next_cursor"]
    if cursor is None: break
check("paging: 4 bookings over 2 pages, oldest first", seen == ["s1", "s2", "s3", "s4"] and pages == 2)
check("limit 0 -> 400", call("GET", "/bookings?email=a@b.co&limit=0")[0] == 400)
check("limit 51 -> 400", call("GET", "/bookings?email=a@b.co&limit=51")[0] == 400)

print("Level 5: rate limit")
call("POST", "/reset", {"slots": slots})
codes = [call("POST", "/bookings", {"slot_id": "nope", "name": "Z", "email": "spam@example.com"}) for _ in range(6)]
check("first 5 attempts are not limited", all(c[0] == 404 for c in codes[:5]))
check("6th -> 429 with Retry-After", codes[5][0] == 429 and codes[5][2].get("Retry-After", "").isdigit())
check("other emails unaffected", call("POST", "/bookings", {"slot_id": "s1", "name": "Q", "email": "q@example.com"})[0] == 201)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
