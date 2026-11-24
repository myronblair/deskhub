#!/usr/bin/env python3
"""Read-only call feed for The Hub (FusionPBX).

Serves JSON like {"calls": [{"t", "dir", "from", "to", "name", "secs"}]} to callers that send the right
bearer token. It runs one fixed SELECT on the call records as a database user that can only read a few
columns. Nothing from the request reaches the command. Calls between extensions are left out.
"""
import hmac
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONF = json.load(open("/opt/hubfeed/conf.json"))
TOKEN = CONF["token"].encode()
QUERY = (
    "SELECT json_build_object('t', start_epoch::bigint, "
    "'dir', CASE WHEN direction = 'outbound' THEN 'out' "
    "            WHEN missed_call IS TRUE OR billsec = 0 THEN 'missed' ELSE 'in' END, "
    "'from', caller_id_number, 'to', destination_number, 'name', COALESCE(caller_id_name, ''), "
    "'secs', billsec::int) "
    "FROM v_xml_cdr WHERE (direction = 'outbound' OR (direction = 'inbound' AND length(caller_id_number) > 5)) "
    "ORDER BY start_epoch DESC LIMIT 10"
)


def calls() -> list[dict]:
    out = subprocess.run(
        ["psql", "-h", "127.0.0.1", "-U", CONF["db_user"], "-d", CONF["db_name"], "-At", "-c", QUERY],
        env={**os.environ, "PGPASSWORD": CONF["db_password"]}, capture_output=True, text=True,
        timeout=15, check=True).stdout
    return [json.loads(line) for line in out.splitlines() if line.strip()]


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        given = self.headers.get("Authorization", "").removeprefix("Bearer ").encode()
        if not hmac.compare_digest(given, TOKEN):
            self.send_response(401); self.end_headers(); return
        try:
            body = json.dumps({"calls": calls()}).encode()
            self.send_response(200)
        except (subprocess.SubprocessError, ValueError) as e:
            print("query failed:", type(e).__name__, file=sys.stderr)
            body = b'{"calls": []}'
            self.send_response(503)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer((CONF.get("bind", "0.0.0.0"), int(CONF.get("port", 8097))), H).serve_forever()
