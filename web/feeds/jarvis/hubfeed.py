#!/usr/bin/env python3
"""Read-only alerts feed for The Hub.

Serves JSON like {"alerts": [{"t", "level", "title", "detail", "resolved"}]} on one port, to callers that
send the right bearer token. It runs one fixed SELECT through the database container as a user that can only
read the alerts table. Nothing from the request reaches the command.
"""
import hmac
import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONF = json.load(open("/opt/hubfeed/conf.json"))
TOKEN = CONF["token"].encode()
QUERY = (
    "SELECT JSON_OBJECT('t', UNIX_TIMESTAMP(created_at), 'severity', severity, 'title', title, "
    "'detail', LEFT(COALESCE(message, ''), 200), 'resolved', resolved) "
    "FROM alerts WHERE resolved = 0 OR resolved_at > NOW() - INTERVAL 6 HOUR "
    "ORDER BY resolved ASC, FIELD(severity, 'critical', 'warning', 'info'), created_at DESC LIMIT 10"
)
LEVEL = {"critical": "crit", "warning": "warn", "info": "info"}


def alerts() -> list[dict]:
    cnf = "[client]\nuser=%s\npassword=%s\n" % (CONF["db_user"], CONF["db_password"])
    out = subprocess.run(
        ["docker", "exec", "-i", "jarvis-db", "mariadb", "--defaults-extra-file=/dev/stdin", "-N", "-B",
         CONF["db_name"], "-e", QUERY],
        input=cnf, capture_output=True, text=True, timeout=15, check=True).stdout
    rows = []
    for line in out.splitlines():
        r = json.loads(line)
        rows.append({"t": r["t"], "level": LEVEL.get(r["severity"], "info"), "title": r["title"],
                     "detail": r["detail"], "resolved": bool(r["resolved"])})
    return rows


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        given = self.headers.get("Authorization", "").removeprefix("Bearer ").encode()
        if not hmac.compare_digest(given, TOKEN):
            self.send_response(401); self.end_headers(); return
        try:
            body = json.dumps({"alerts": alerts()}).encode()
            self.send_response(200)
        except (subprocess.SubprocessError, ValueError, KeyError) as e:
            print("query failed:", type(e).__name__, file=sys.stderr)
            body = b'{"alerts": []}'
            self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer((CONF.get("bind", "0.0.0.0"), int(CONF.get("port", 8097))), H).serve_forever()
