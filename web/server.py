#!/usr/bin/env python3
"""Desk Hub web board: a virtual hub for browsers and tablets.

The Windows companion connects here exactly as it connects to the ESP32 board
(set `host` in its [hub] section to this machine). This server remembers the
latest message of each kind and relays everything to web pages at
`/?token=...`. Taps on the page (media, shortcuts, Steam, alerts, speed test)
go back to the companion.

Settings come from the environment:
    DESKHUB_TOKEN     required, the companion token (also used by the web page)
    DESKHUB_PORT      default 80
    DESKHUB_BIND      default 0.0.0.0
    DESKHUB_CONTROLS  1 (default) lets the page send commands; 0 makes it view-only
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
    def __init__(self, token: str, controls: bool):
        self.token = token
        self.controls = controls
        self.latest: dict[str, str] = {}
        self.art: dict[str, str] = {}
        self.viewers: set[web.WebSocketResponse] = set()
        self.companion: web.WebSocketResponse | None = None

    def ok(self, given: str) -> bool:
        return hmac.compare_digest(given.encode(), self.token.encode())

    def status(self) -> str:
        return json.dumps({"type": "_status", "companion": self.companion is not None,
                           "controls": self.controls})

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
        if not self.ok(request.query.get("token", "")):
            raise web.HTTPUnauthorized(text="bad token")
        ws = web.WebSocketResponse(heartbeat=15, max_msg_size=2**16)
        await ws.prepare(request)
        self.viewers.add(ws)
        try:
            await ws.send_str(self.status())
            for text in list(self.latest.values()) + list(self.art.values()):
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
    board = Board(token, os.environ.get("DESKHUB_CONTROLS", "1") != "0")
    app = web.Application()
    app.add_routes([web.get("/", board.page), web.get("/ws", board.companion_ws),
                    web.get("/view", board.viewer_ws), web.get("/healthz", board.health)])
    web.run_app(app, host=os.environ.get("DESKHUB_BIND", "0.0.0.0"),
                port=int(os.environ.get("DESKHUB_PORT", "80")), print=None)


if __name__ == "__main__":
    main()
