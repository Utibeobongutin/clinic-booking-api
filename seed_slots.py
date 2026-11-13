"""
Puts some demo appointment times into your API so the web page has something to show.
    python seed_slots.py http://localhost:5000 your-secret-key
WARNING: this erases existing slots and bookings (it calls /reset).
"""
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

base, key = sys.argv[1].rstrip("/"), sys.argv[2]
start = (datetime.now(timezone.utc) + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
slots = []
for day in range(3):
    for i, provider in enumerate(["Dr Ade", "Dr Bola"]):
        for n in range(3):
            s = start + timedelta(days=day, hours=n)
            slots.append({"id": f"{provider.split()[1].lower()}-{day}-{n}", "provider": provider,
                          "start": s.strftime("%Y-%m-%dT%H:%M:%SZ"),
                          "end": (s + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")})
req = urllib.request.Request(base + "/reset", data=json.dumps({"slots": slots}).encode(), method="POST",
                             headers={"X-API-Key": key, "Content-Type": "application/json"})
print(urllib.request.urlopen(req, timeout=60).read().decode(), f"- added {len(slots)} slots")
