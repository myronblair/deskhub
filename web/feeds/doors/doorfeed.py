#!/usr/bin/env python3
"""Read-only door events feed for The Hub (Portcullis).

Serves JSON like {"events": [{"t", "door", "who", "result"}]} to callers that send the right bearer token.
It only reads: the stored events (SQLite, opened read-only) and the door and card names from the access
system's store. It never talks to the door controller and never writes anything. The service runs inside a
systemd sandbox where those paths are read-only.
"""
import datetime as dt
import hmac
import json
import sqlite3
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

CONF = json.load(open("/opt/hubfeed/conf.json"))
TOKEN = CONF["token"].encode()
DB = "file:/var/lib/portcullis/portcullis.db?mode=ro"
STORE = "/var/uhppoted/httpd/system/"
TZ = ZoneInfo(CONF.get("tz", "America/Chicago"))   # the stored event times are the controller's local time
CACHE_S = 5
_cache = {"at": 0.0, "body": b'{"events": []}'}


def names():
    """({card number: holder name}, {(controller serial, door position): door name})."""
    def load(name, key):
        try:
            return json.load(open(STORE + name)).get(key, [])
        except (OSError, ValueError):
            return []
    cards = {str(c.get("card")): str(c.get("name") or "") for c in load("cards.json", "cards")}
    door_names = {d.get("OID"): str(d.get("name") or "") for d in load("doors.json", "doors")}
    doors = {}
    for c in load("controllers.json", "controllers"):
        serial = str(c.get("device-id"))
        for pos, oid in (c.get("doors") or {}).items():
            doors[(serial, str(pos))] = door_names.get(oid, "")
    return cards, doors


def events() -> list[dict]:
    cards, doors = names()
    con = sqlite3.connect(DB, uri=True, timeout=3)
    try:
        rows = con.execute("SELECT ts, card, door, granted, event_type, controller FROM events "
                           "ORDER BY ts DESC, idx DESC LIMIT 10").fetchall()
    finally:
        con.close()
    out = []
    for ts, card, door, granted, etype, serial in rows:
        try:
            t = int(dt.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ).timestamp())
        except (TypeError, ValueError):
            continue
        name = doors.get((str(serial), str(door))) or (f"Door {door}" if door else "")
        if etype == 1 and card:
            who = cards.get(str(card)) or f"Unknown card {card}"
            result = "granted" if granted else "denied"
        else:
            who, result = "Door event", "other"
        out.append({"t": t, "door": name, "who": who, "result": result})
    return out


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        given = self.headers.get("Authorization", "").removeprefix("Bearer ").encode()
        if not hmac.compare_digest(given, TOKEN):
            self.send_response(401); self.end_headers(); return
        status = 200
        if time.time() - _cache["at"] > CACHE_S:
            try:
                _cache["body"] = json.dumps({"events": events()}).encode()
                _cache["at"] = time.time()
            except (sqlite3.Error, OSError, ValueError) as e:
                print("read failed:", type(e).__name__, file=sys.stderr)
                status = 503     # a stale copy is still served below
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(_cache["body"])))
        self.end_headers()
        self.wfile.write(_cache["body"])

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer((CONF.get("bind", "0.0.0.0"), int(CONF.get("port", 8097))), H).serve_forever()
