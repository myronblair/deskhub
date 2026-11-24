#!/usr/bin/env python3
"""The Hub: a virtual hub for browsers and tablets.

The Windows companion connects here exactly as it connects to the ESP32 board
(set `host` in its [hub] section to this machine). This server remembers the
latest message of each kind and relays everything to web pages at
`/?token=...`. Taps on the page (media, shortcuts, Steam, alerts, speed test)
go back to the companion.

Settings come from the environment:
    DESKHUB_TOKEN     required, the companion token (also used by the web page)
    DESKHUB_PORT      default 80
    DESKHUB_BIND      default 0.0.0.0
    DESKHUB_PIN       optional short code for the web page (4+ characters), so nobody types the token
    DESKHUB_CONTROLS  1 (default) lets the page send commands; 0 makes it view-only
    DESKHUB_FEEDS     path of a JSON file with read-only data feeds (default /etc/deskhub-feeds.json):
                      {"nas": {"host": "10.0.0.9", "conf_dir": "/etc/deskhub-snmp"},
                       "doors": {"url": "http://...", "token": "..."},
                       "calls": {"url": "...", "token": "..."},
                       "jarvis": {"url": "...", "token": "..."},
                       "ha": {"url": "http://10.0.0.7:8123", "token": "<long-lived access token>",
                              "toggle": ["light.porch"], "watch": ["binary_sensor.shed_door"]}}
    DESKHUB_CHECKS    hosts to watch, e.g. "Proxmox=10.0.0.5:8006,NAS=10.0.0.9:5001" (shown as Lab status)
    DESKHUB_LAT, DESKHUB_LON, DESKHUB_UNIT   weather place for the page (unit: f or c)
"""
import array
import asyncio
import base64
import hmac
import json
import logging
import os
import pathlib
import re
import struct
import sys
import time
import zlib

import aiohttp
from aiohttp import WSMsgType, web

log = logging.getLogger("deskhub-web")
HERE = pathlib.Path(__file__).resolve().parent
FROM_PAGE = {"media", "speedtest", "steam_launch", "shortcut", "alert_action"}
MAX_ART = 48
VERSION = "web-1.0"

_R5 = [i * 255 // 31 for i in range(32)]
_G6 = [i * 255 // 63 for i in range(64)]


def png_from_565(w: int, h: int, pixels: bytes) -> bytes:
    """A PNG from little-endian RGB565 pixels."""
    px = array.array("H")
    px.frombytes(pixels[: w * h * 2])
    if sys.byteorder == "big":
        px.byteswap()
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        for v in px[y * w:(y + 1) * w]:
            raw += bytes((_R5[v >> 11], _G6[(v >> 5) & 63], _R5[v & 31]))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))


def art_message(frame: bytes) -> tuple[str, str] | None:
    """(replay key, JSON text) for a cover-art frame from the companion."""
    try:
        if frame[:4] == b"ART1":
            art_id, w, h = struct.unpack("<IHH", frame[4:12])
            msg = {"type": "_art", "kind": "media", "id": art_id}
            body = frame[12:]
            key = "media"
        elif frame[:4] == b"STM1":
            appid, w, h, kind, _ = struct.unpack("<IHHHH", frame[4:16])
            msg = {"type": "_art", "kind": "steam", "appid": appid, "k": kind}
            body = frame[16:]
            key = f"steam:{appid}:{kind}"
        else:
            return None
        if not (0 < w <= 1024 and 0 < h <= 1024) or len(body) < w * h * 2:
            return None
        msg["png"] = base64.b64encode(png_from_565(w, h, body)).decode()
        return key, json.dumps(msg, separators=(",", ":"))
    except (struct.error, ValueError):
        return None


SETUP_PS1 = r'''# Set up the companion on this PC and connect it to The Hub. Safe to run again.
$ErrorActionPreference = "Stop"
$dir = Join-Path $env:USERPROFILE "deskhub"
$comp = Join-Path $dir "companion"

# 1. uv (runs the companion and installs what it needs)
$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) {
    Write-Host "Installing uv..."
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $uv = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
}

# 2. The companion code (public, no git needed). Your config.toml is kept.
Write-Host "Downloading the companion..."
$zip = Join-Path $env:TEMP "deskhub.zip"
$tmp = Join-Path $env:TEMP "deskhub-extract"
Invoke-WebRequest "https://github.com/myronblair/deskhub/archive/refs/heads/main.zip" -OutFile $zip
if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
Expand-Archive $zip -DestinationPath $tmp
New-Item -ItemType Directory -Force $dir | Out-Null
Copy-Item (Join-Path $tmp "deskhub-main\*") $dir -Recurse -Force
Remove-Item $tmp -Recurse -Force; Remove-Item $zip -Force

# 3. Settings: where The Hub is. Written once; edit config.toml later to add more.
$cfg = Join-Path $comp "config.toml"
if (-not (Test-Path $cfg)) {
    $text = @"
[hub]
host = "__HOST__"
token = "__TOKEN__"

[calendar]
days = 7

[claude]
live_limits = false
alerts = true
idle_alert_seconds = 30
"@
    [IO.File]::WriteAllText($cfg, $text, (New-Object Text.UTF8Encoding $false))
    Write-Host "Wrote $cfg"
} else {
    Write-Host "Kept your existing $cfg (check its [hub] host and token)"
}

# 4. Install and start (hidden, with an icon in the notification area)
Write-Host "Installing the companion's parts (the first time takes a minute or two)..."
Push-Location $comp
& $uv sync
Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -like "*-m deskhub*" } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-Process -FilePath $uv -ArgumentList "run", "python", "-m", "deskhub", "--tray" -WorkingDirectory $comp -WindowStyle Hidden
Pop-Location
Write-Host "Started. The Hub should say PC connected in a few seconds."
'''


# ---------------------------------------------------------------- read-only feeds
SNMP_ROOTS = ["1.3.6.1.4.1.6574", "1.3.6.1.4.1.2021.11", "1.3.6.1.4.1.2021.4", "1.3.6.1.2.1.25.2.3.1",
              "1.3.6.1.2.1.25.1.1"]
DISK_STATUS = {1: "ok", 2: "warn", 3: "warn", 4: "bad", 5: "bad"}
RAID_STATUS = {1: "ok", 2: "repairing", 3: "migrating", 4: "expanding", 5: "deleting", 6: "creating",
               7: "syncing", 8: "parity check", 9: "assembling", 10: "canceling", 11: "degraded",
               12: "crashed", 13: "scrubbing"}


def parse_snmp(text: str) -> dict[str, str]:
    """{numeric oid: value} from `snmpwalk -Oqnt` output."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^\.?(\d[\d.]*)\s+(.*)$", line.strip())
        if m:
            out[m.group(1)] = m.group(2).strip().strip('"')
    return out


def _int(v, default=None):
    try:
        return int(float(str(v).split()[0]))
    except (ValueError, TypeError, IndexError):
        return default


def nas_message(v: dict[str, str]) -> dict:
    """A Synology's SNMP values as the page's NAS card."""
    def col(prefix: str) -> dict[str, str]:
        n = len(prefix) + 1
        return {k[n:]: val for k, val in v.items() if k.startswith(prefix + ".")}

    syn = "1.3.6.1.4.1.6574"
    cpu = (_int(v.get("1.3.6.1.4.1.2021.11.9.0"), 0) or 0) + (_int(v.get("1.3.6.1.4.1.2021.11.10.0"), 0) or 0)
    total = _int(v.get("1.3.6.1.4.1.2021.4.5.0"), 0) or 0
    free = sum(_int(v.get(f"1.3.6.1.4.1.2021.4.{i}.0"), 0) or 0 for i in (6, 14, 15))
    names, units, sizes, used = (col(f"1.3.6.1.2.1.25.2.3.1.{i}") for i in (3, 4, 5, 6))
    vols = []
    for i, name in names.items():
        if name.startswith("/volume"):
            unit, size, use = _int(units.get(i), 0) or 0, _int(sizes.get(i), 0) or 0, _int(used.get(i), 0) or 0
            if size > 0:
                vols.append({"name": name.lstrip("/"), "pct": round(use / size * 100, 1),
                             "size_gb": round(size * unit / 1e9), "used_gb": round(use * unit / 1e9)})
    dn, dm, ds, dt = (col(f"{syn}.2.1.1.{i}") for i in (2, 3, 5, 6))
    disks = [{"name": dn[i][:24], "model": dm.get(i, "")[:30], "state": DISK_STATUS.get(_int(ds.get(i), 1), "ok"),
              "temp": _int(dt.get(i))} for i in sorted(dn, key=lambda x: _int(x, 0))]
    rn, rs = col(f"{syn}.3.1.1.2"), col(f"{syn}.3.1.1.3")
    raids = [{"name": rn[i][:24], "state": RAID_STATUS.get(_int(rs.get(i), 1), "unknown")}
             for i in sorted(rn, key=lambda x: _int(x, 0))]
    up = _int(v.get("1.3.6.1.2.1.25.1.1.0"))
    return {"type": "_nas", "ok": _int(v.get(f"{syn}.1.1.0"), 1) == 1, "temp": _int(v.get(f"{syn}.1.2.0")),
            "cpu": min(100, cpu), "mem": round((1 - free / total) * 100, 1) if total else None,
            "uptime": up // 100 if up is not None else None, "volumes": vols, "disks": disks, "raids": raids}


def _s(v, n):
    return str(v if v is not None else "")[:n]


def clean_feed(kind: str, data: dict) -> dict:
    """The page's message for a doors, calls or jarvis feed. Only the expected fields are kept."""
    rows = data.get({"doors": "events", "calls": "calls", "jarvis": "alerts"}[kind]) or []
    out = []
    for r in rows[:10] if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        t = _int(r.get("t"), 0)
        if kind == "doors":
            res = r.get("result") if r.get("result") in ("granted", "denied") else "other"
            out.append({"t": t, "door": _s(r.get("door"), 40), "who": _s(r.get("who"), 60), "result": res})
        elif kind == "calls":
            d = r.get("dir") if r.get("dir") in ("in", "out", "missed") else "in"
            out.append({"t": t, "dir": d, "from": _s(r.get("from"), 40), "to": _s(r.get("to"), 40),
                        "name": _s(r.get("name"), 60), "secs": _int(r.get("secs"), 0)})
        else:
            lv = r.get("level") if r.get("level") in ("info", "warn", "crit") else "info"
            out.append({"t": t, "level": lv, "title": _s(r.get("title"), 90), "detail": _s(r.get("detail"), 160),
                        "resolved": bool(r.get("resolved"))})
    return {"type": "_" + kind, "items": out}


HA_TOGGLE_DOMAINS = {"light", "switch", "fan", "input_boolean"}   # never locks, covers, alarms or buttons


def ha_message(states: list, toggle: list, watch: list) -> dict:
    """The page's Home card from Home Assistant's /api/states, limited to the configured entities."""
    by_id = {x.get("entity_id"): x for x in states if isinstance(x, dict)}
    items = []
    for kind, ids in (("toggle", toggle), ("sensor", watch)):
        for eid in ids:
            x = by_id.get(eid)
            if not x:
                continue
            attrs = x.get("attributes") or {}
            items.append({"id": eid, "kind": kind, "name": _s(attrs.get("friendly_name") or eid, 60),
                          "state": _s(x.get("state"), 20), "dc": _s(attrs.get("device_class"), 20)})
    return {"type": "_ha", "items": items}


class Board:
    def __init__(self, token: str, controls: bool, pin: str = "", place: dict | None = None,
                 checks: list | None = None):
        self.checks = checks or []          # [(name, host, port)]
        self.extra: dict[str, str] = {}      # latest feed messages (lab checks, NAS, doors...)
        self.feeds: dict = {}
        self.token = token
        self.pin = pin
        self.controls = controls
        self.place = place or {}
        self.latest: dict[str, str] = {}
        self.art: dict[str, str] = {}
        self.viewers: set[web.WebSocketResponse] = set()
        self.companion: web.WebSocketResponse | None = None

    def ok(self, given: str) -> bool:
        return hmac.compare_digest(given.encode(), self.token.encode())

    def ok_view(self, given: str) -> bool:
        return self.ok(given) or bool(self.pin and hmac.compare_digest(given.encode(), self.pin.encode()))

    def status(self) -> str:
        return json.dumps({"type": "_status", "companion": self.companion is not None,
                           "controls": self.controls, **self.place})

    async def broadcast(self, text: str):
        for v in list(self.viewers):
            try:
                await v.send_str(text)
            except (ConnectionError, RuntimeError):
                self.viewers.discard(v)

    async def companion_ws(self, request: web.Request):
        ws = web.WebSocketResponse(heartbeat=10, max_msg_size=2**21)
        await ws.prepare(request)
        try:
            first = await asyncio.wait_for(ws.receive(), 10)
            hello = json.loads(first.data) if first.type == WSMsgType.TEXT else {}
        except (asyncio.TimeoutError, ValueError):
            hello = {}
        if hello.get("type") != "hello" or not self.ok(str(hello.get("token", ""))):
            await ws.close(code=4001, message=b"bad token")
            return ws
        if self.companion is not None:
            await self.companion.close()
        self.companion = ws
        # A new session resends everything, so drop what the last one left.
        self.latest.clear()
        self.art.clear()
        await ws.send_str(json.dumps({"type": "hello_ok", "version": VERSION}))
        log.info("companion connected from %s", request.remote)
        await self.broadcast(self.status())
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        kind = json.loads(msg.data).get("type")
                    except ValueError:
                        continue
                    if isinstance(kind, str):
                        self.latest[kind] = msg.data
                        await self.broadcast(msg.data)
                elif msg.type == WSMsgType.BINARY:
                    made = art_message(msg.data)
                    if made:
                        key, text = made
                        if key not in self.art and len(self.art) >= MAX_ART:
                            self.art.pop(next(iter(self.art)))
                        self.art[key] = text
                        await self.broadcast(text)
        finally:
            if self.companion is ws:
                self.companion = None
                log.info("companion disconnected")
                await self.broadcast(self.status())
        return ws

    async def viewer_ws(self, request: web.Request):
        if not self.ok_view(request.query.get("token", "")):
            raise web.HTTPUnauthorized(text="bad token")
        ws = web.WebSocketResponse(heartbeat=15, max_msg_size=2**16)
        await ws.prepare(request)
        self.viewers.add(ws)
        try:
            await ws.send_str(self.status())
            for text in list(self.extra.values()) + list(self.latest.values()) + list(self.art.values()):
                await ws.send_str(text)
            async for msg in ws:
                if msg.type != WSMsgType.TEXT or not self.controls:
                    continue
                try:
                    cmd = json.loads(msg.data)
                except ValueError:
                    continue
                if not isinstance(cmd, dict):
                    continue
                if cmd.get("type") == "ha_toggle":
                    await self.ha_toggle(str(cmd.get("id", "")))
                elif cmd.get("type") in FROM_PAGE and self.companion is not None:
                    await self.companion.send_str(json.dumps(cmd, separators=(",", ":")))
        finally:
            self.viewers.discard(ws)
        return ws

    async def probe(self, name: str, host: str, port: int) -> dict:
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection(host, port), 2.5)
            w.close()
            return {"name": name, "up": True, "ms": round((loop.time() - t0) * 1000)}
        except (OSError, asyncio.TimeoutError):
            return {"name": name, "up": False}

    async def check_loop(self):
        while True:
            results = await asyncio.gather(*(self.probe(*c) for c in self.checks))
            await self.publish({"type": "_checks", "items": results})
            await asyncio.sleep(30)

    async def publish(self, msg: dict):
        text = json.dumps(msg, separators=(",", ":"))
        self.extra[msg["type"]] = text
        await self.broadcast(text)

    async def snmp_walk(self, conf_dir: str, host: str, root: str) -> str:
        env = {**os.environ, "SNMPCONFPATH": conf_dir, "MIBS": ""}
        proc = await asyncio.create_subprocess_exec("snmpwalk", "-Oqnt", "-t", "3", "-r", "1", host, root,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=env)
        out, _ = await asyncio.wait_for(proc.communicate(), 20)
        return out.decode("utf-8", "replace")

    async def nas_loop(self, cfg: dict):
        host, conf = str(cfg["host"]), str(cfg.get("conf_dir", "/etc/deskhub-snmp"))
        while True:
            try:
                vals: dict[str, str] = {}
                for root in SNMP_ROOTS:
                    vals.update(parse_snmp(await self.snmp_walk(conf, host, root)))
                if not vals:
                    raise RuntimeError("no SNMP answer")
                await self.publish(nas_message(vals))
            except (OSError, RuntimeError, asyncio.TimeoutError) as e:
                log.warning("NAS feed: %s", e)
                await self.publish({"type": "_nas", "offline": True})
            await asyncio.sleep(30)

    async def http_feed_loop(self, kind: str, cfg: dict):
        headers = {"Authorization": "Bearer " + str(cfg.get("token", ""))}
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            while True:
                try:
                    async with sess.get(str(cfg["url"]), headers=headers) as r:
                        r.raise_for_status()
                        await self.publish(clean_feed(kind, await r.json(content_type=None)))
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError) as e:
                    log.warning("%s feed: %s", kind, e)
                    await self.publish({"type": "_" + kind, "offline": True})
                await asyncio.sleep(10)

    async def ha_fetch(self, sess, cfg: dict) -> dict:
        async with sess.get(cfg["url"].rstrip("/") + "/api/states", headers=self.ha_headers) as r:
            r.raise_for_status()
            return ha_message(await r.json(content_type=None), cfg.get("toggle", []), cfg.get("watch", []))

    async def ha_toggle(self, entity: str):
        """Toggle one Home Assistant entity, but only if it is on the configured list and a safe kind."""
        cfg = self.feeds.get("ha") or {}
        if entity not in cfg.get("toggle", []) or entity.split(".", 1)[0] not in HA_TOGGLE_DOMAINS:
            log.warning("ha_toggle refused: %r", entity[:60])
            return
        domain = entity.split(".", 1)[0]
        try:
            async with self.ha_sess.post(cfg["url"].rstrip("/") + f"/api/services/{domain}/toggle", headers=self.ha_headers,
                                         json={"entity_id": entity}) as r:
                r.raise_for_status()
            await asyncio.sleep(0.6)
            await self.publish(await self.ha_fetch(self.ha_sess, cfg))
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            log.warning("ha toggle failed: %s", e)

    async def ha_loop(self, cfg: dict):
        self.ha_headers = {"Authorization": "Bearer " + str(cfg.get("token", ""))}
        self.ha_sess = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        try:
            while True:
                try:
                    await self.publish(await self.ha_fetch(self.ha_sess, cfg))
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, KeyError) as e:
                    log.warning("ha feed: %s", e)
                    await self.publish({"type": "_ha", "offline": True})
                await asyncio.sleep(5)
        finally:
            await self.ha_sess.close()

    async def start_checks(self, app):
        tasks = []
        if self.checks:
            tasks.append(asyncio.create_task(self.check_loop()))
        if self.feeds.get("nas"):
            tasks.append(asyncio.create_task(self.nas_loop(self.feeds["nas"])))
        if self.feeds.get("ha"):
            tasks.append(asyncio.create_task(self.ha_loop(self.feeds["ha"])))
        for kind in ("doors", "calls", "jarvis"):
            if self.feeds.get(kind):
                tasks.append(asyncio.create_task(self.http_feed_loop(kind, self.feeds[kind])))
        app["feed_tasks"] = tasks

    async def stop_checks(self, app):
        for t in app.get("feed_tasks", []):
            t.cancel()

    async def setup_script(self, request: web.Request):
        """A PowerShell script that sets up the companion on a PC, with this hub's address and token."""
        if not self.ok_view(request.query.get("code", "")):
            raise web.HTTPUnauthorized(text="bad code")
        text = (SETUP_PS1.replace("__HOST__", request.host.split(":")[0] if request.host else "")
                .replace("__TOKEN__", self.token))
        return web.Response(text=text, content_type="text/plain", headers={"Cache-Control": "no-store"})

    async def page(self, request: web.Request):
        return web.FileResponse(HERE / "index.html", headers={"Cache-Control": "no-cache"})

    async def health(self, request: web.Request):
        return web.json_response({"ok": True, "companion": self.companion is not None,
                                  "viewers": len(self.viewers)})


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ.get("DESKHUB_TOKEN", "")
    if len(token) < 8:
        sys.exit("Set DESKHUB_TOKEN (at least 8 characters).")
    pin = os.environ.get("DESKHUB_PIN", "")
    if pin and len(pin) < 4:
        sys.exit("DESKHUB_PIN must be at least 4 characters.")
    place = {}
    if os.environ.get("DESKHUB_LAT") and os.environ.get("DESKHUB_LON"):
        try:
            place = {"lat": float(os.environ["DESKHUB_LAT"]), "lon": float(os.environ["DESKHUB_LON"]),
                     "unit": "c" if os.environ.get("DESKHUB_UNIT", "f").lower() == "c" else "f"}
        except ValueError:
            sys.exit("DESKHUB_LAT and DESKHUB_LON must be numbers.")
    checks = []
    for item in filter(None, (c.strip() for c in os.environ.get("DESKHUB_CHECKS", "").split(","))):
        try:
            name, addr = item.split("=", 1)
            host, port = addr.rsplit(":", 1)
            checks.append((name.strip(), host.strip(), int(port)))
        except ValueError:
            sys.exit(f"DESKHUB_CHECKS: '{item}' should look like Name=host:port")
    board = Board(token, os.environ.get("DESKHUB_CONTROLS", "1") != "0", pin, place, checks)
    feeds_path = pathlib.Path(os.environ.get("DESKHUB_FEEDS", "/etc/deskhub-feeds.json"))
    if feeds_path.is_file():
        try:
            board.feeds = json.loads(feeds_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            sys.exit(f"{feeds_path}: {e}")
    app = web.Application()
    app.on_startup.append(board.start_checks)
    app.on_cleanup.append(board.stop_checks)
    app.add_routes([web.get("/", board.page), web.get("/ws", board.companion_ws),
                    web.get("/view", board.viewer_ws), web.get("/healthz", board.health),
                    web.get("/setup.ps1", board.setup_script)])
    web.run_app(app, host=os.environ.get("DESKHUB_BIND", "0.0.0.0"),
                port=int(os.environ.get("DESKHUB_PORT", "80")), print=None)


if __name__ == "__main__":
    main()
