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
import struct
import sys
import zlib

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


class Board:
    def __init__(self, token: str, controls: bool, pin: str = "", place: dict | None = None,
                 checks: list | None = None):
        self.checks = checks or []          # [(name, host, port)]
        self.checks_msg = ""
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
            for text in ([self.checks_msg] if self.checks_msg else []) + list(self.latest.values()) + list(self.art.values()):
                await ws.send_str(text)
            async for msg in ws:
                if msg.type != WSMsgType.TEXT or not self.controls or self.companion is None:
                    continue
                try:
                    cmd = json.loads(msg.data)
                except ValueError:
                    continue
                if isinstance(cmd, dict) and cmd.get("type") in FROM_PAGE:
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
            self.checks_msg = json.dumps({"type": "_checks", "items": results}, separators=(",", ":"))
            await self.broadcast(self.checks_msg)
            await asyncio.sleep(30)

    async def start_checks(self, app):
        if self.checks:
            app["checks"] = asyncio.create_task(self.check_loop())

    async def stop_checks(self, app):
        if "checks" in app:
            app["checks"].cancel()

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
    app = web.Application()
    app.on_startup.append(board.start_checks)
    app.on_cleanup.append(board.stop_checks)
    app.add_routes([web.get("/", board.page), web.get("/ws", board.companion_ws),
                    web.get("/view", board.viewer_ws), web.get("/healthz", board.health)])
    web.run_app(app, host=os.environ.get("DESKHUB_BIND", "0.0.0.0"),
                port=int(os.environ.get("DESKHUB_PORT", "80")), print=None)


if __name__ == "__main__":
    main()
