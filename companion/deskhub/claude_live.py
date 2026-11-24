"""Opt-in: current Claude plan limits straight from Anthropic.

Off unless `[claude] live_limits = true` in config.toml. When on, the companion
reads the access token that Claude Code saved in ~/.claude/.credentials.json and
asks https://api.anthropic.com/api/oauth/usage for the 5-hour and weekly
percentages, the same lookup Claude's own apps use to draw their usage bars.

- No messages are sent to Claude, so this uses no tokens and no API credits.
- The token stays in memory. It is never logged, saved elsewhere, or sent to the hub.
- The token is never refreshed here (refreshing it ourselves could sign Claude Code
  out). When it expires, the companion runs `claude auth status`, which has Claude
  Code renew its own sign-in without sending a message; at most every 10 minutes.
  The Claude desktop app and IDE extensions don't renew this file, so without that
  live limits would stop after a few hours of not using `claude` in a terminal.
- The endpoint is undocumented and may change; failures fall back quietly to the
  status line readings.
"""
import datetime as dt
import json
import logging
import os
import pathlib
import shutil
import subprocess
import sys
import time

import httpx

log = logging.getLogger(__name__)

URL = "https://api.anthropic.com/api/oauth/usage"
CREDENTIALS = pathlib.Path.home() / ".claude" / ".credentials.json"
RENEW_EVERY = 600   # seconds between attempts to have Claude Code renew its sign-in


def find_claude() -> str | None:
    """The `claude` CLI: on PATH, or where its installer puts it."""
    found = shutil.which("claude")
    if found:
        return found
    for p in (pathlib.Path.home() / ".local" / "bin" / "claude.exe",
              pathlib.Path.home() / ".local" / "bin" / "claude"):
        if p.is_file():
            return str(p)
    return None


def renew_signin(claude: str | None, config_dir: pathlib.Path | None = None) -> bool:
    """Ask Claude Code to renew its sign-in. `claude auth status` refreshes an
    expired token and writes it back to the credentials file itself; it sends no
    message to Claude. Its output (which names the account) is discarded."""
    if not claude:
        return False
    try:
        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(config_dir)} if config_dir else None
        r = subprocess.run([claude, "auth", "status"], capture_output=True, timeout=60, env=env,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def _epoch(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return int(dt.datetime.fromisoformat(iso).timestamp())
    except ValueError:
        return None


class LiveLimits:
    def __init__(self, path: pathlib.Path = CREDENTIALS, renew=None):
        self.path = path
        self._next = 0.0
        self._last: dict = {}
        self._bad_mtime = None   # credentials file state that got a 401
        self._state = ""         # last logged problem, so each is logged once
        # Called to have Claude Code renew an expired sign-in; returns True if it ran.
        self._renew = renew if renew is not None else (lambda: renew_signin(find_claude()))
        self._next_renew = 0.0

    def _try_renew(self) -> bool:
        now = time.time()
        if now < self._next_renew:
            return False
        self._next_renew = now + RENEW_EVERY
        if self._renew():
            log.info("live Claude limits: asked Claude Code to renew its sign-in")
            return True
        return False

    def _note(self, state: str, msg: str):
        if state != self._state:
            self._state = state
            if msg:
                log.info("live Claude limits: %s", msg)

    def _read(self):
        """(access token, expiry, file mtime) from the credentials file."""
        st = self.path.stat()
        oauth = json.loads(self.path.read_text(encoding="utf-8")).get("claudeAiOauth") or {}
        return oauth.get("accessToken"), (oauth.get("expiresAt") or 0) / 1000, st.st_mtime

    def _token(self, renewed: bool = False) -> str | None:
        try:
            token, expires, mtime = self._read()
        except (OSError, ValueError):
            self._note("nofile", "no Claude Code sign-in found; run `claude` once to sign in")
            return None
        if not token:
            self._note("notoken", "no access token in the Claude Code sign-in")
            return None
        stale = self._bad_mtime == mtime or (expires and expires < time.time() + 30)
        if stale:
            # Rejected or expired: have Claude Code renew it, then read it again.
            if not renewed and self._try_renew():
                return self._token(renewed=True)
            self._note("expired", "sign-in expired; paused until Claude Code renews it (use `claude` once)")
            return None
        return token

    def get(self, active: bool) -> dict:
        """Latest limits as {five_hour: {pct, reset}, seven_day: {...}, updated}, or {}.
        Polls every minute while Claude Code is in use, every 5 minutes otherwise."""
        now = time.time()
        if now < self._next:
            return self._last
        self._next = now + (60 if active else 300)
        token = self._token()
        if not token:
            return self._last if self._fresh() else {}
        try:
            r = httpx.get(URL, timeout=15, headers={
                "Authorization": f"Bearer {token}",
                "anthropic-beta": "oauth-2025-04-20",
                "User-Agent": "deskhub",
            })
        except httpx.HTTPError as e:
            self._note("neterr", f"request failed ({type(e).__name__})")
            return self._last if self._fresh() else {}
        if r.status_code == 401:
            try:
                self._bad_mtime = self.path.stat().st_mtime
            except OSError:
                pass
            self._note("401", "sign-in was rejected; paused until Claude Code renews it")
            return {}
        if r.status_code == 429:
            self._next = now + 600
            self._note("429", "rate limited; trying again in 10 minutes")
            return self._last if self._fresh() else {}
        if r.status_code != 200:
            self._note(f"http{r.status_code}", f"HTTP {r.status_code}")
            return self._last if self._fresh() else {}
        try:
            data = r.json()
        except ValueError:
            self._note("badjson", "unexpected response")
            return {}
        out = {}
        for name in ("five_hour", "seven_day"):
            w = data.get(name) or {}
            pct, reset = w.get("utilization"), _epoch(w.get("resets_at"))
            if isinstance(pct, (int, float)) and reset and reset > now:
                out[name] = {"pct": round(float(pct), 1), "reset": reset}
        if out:
            out["updated"] = int(now)
            self._note("ok", "receiving live limits")
        self._last = out
        return out

    def _fresh(self) -> bool:
        return bool(self._last) and time.time() - self._last.get("updated", 0) < 15 * 60
