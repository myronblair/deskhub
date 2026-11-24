"""Claude Code token usage, read from the local session transcripts.

Claude Code writes one JSONL file per session under ~/.claude/projects/. Each
assistant message carries the API `usage` object. Only token counts, model
names and timestamps are read; message text is never looked at or sent.

The transcript format is not a documented API, so this may need updating if
Claude Code changes it.
"""
import datetime as dt
import json
import logging
import pathlib
import time

log = logging.getLogger(__name__)

BLOCK = dt.timedelta(hours=5)
HISTORY_DAYS = 7


STATUS_FILE = pathlib.Path.home() / ".deskhub" / "claude_status.json"


def read_limits() -> dict:
    """Plan limits saved by statusline_bridge.py (Claude Code's documented
    `rate_limits` status line data). Windows whose reset time has passed are dropped."""
    try:
        data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    now = time.time()
    out = {}
    for name in ("five_hour", "seven_day"):
        w = (data.get("rate_limits") or {}).get(name) or {}
        pct, reset = w.get("used_percentage"), w.get("resets_at")
        if isinstance(pct, (int, float)) and isinstance(reset, (int, float)) and reset > now:
            out[name] = {"pct": round(float(pct), 1), "reset": int(reset)}
    if out:
        out["updated"] = int(data.get("updated", 0))
    return out


def _model_family(model: str) -> str:
    m = (model or "").lower()
    for fam in ("fable", "opus", "sonnet", "haiku"):
        if fam in m:
            return fam.title()
    return "Other"


class ClaudeUsage:
    def __init__(self, root: pathlib.Path | None = None, live=None, bridge: bool = True):
        self.live = live  # optional claude_live.LiveLimits
        self.bridge = bridge  # read the status-line file; only right for the default login
        self.root = root or pathlib.Path.home() / ".claude" / "projects"
        self._offsets: dict[pathlib.Path, int] = {}
        self._seen: set[str] = set()
        # (timestamp, family, input, output, cache_create, cache_read)
        self._entries: list[tuple[dt.datetime, str, int, int, int, int]] = []

    def _scan(self):
        if not self.root.is_dir():
            return
        cutoff = time.time() - (HISTORY_DAYS + 1) * 86400
        for path in self.root.glob("**/*.jsonl"):
            try:
                st = path.stat()
            except OSError:
                continue
            if st.st_mtime < cutoff:
                continue
            start = self._offsets.get(path, 0)
            if st.st_size < start:  # file was rewritten
                start = 0
            if st.st_size == start:
                continue
            try:
                with open(path, "rb") as f:
                    f.seek(start)
                    data = f.read()
            except OSError:
                continue
            # Only consume complete lines; a partial last line is re-read next time.
            end = data.rfind(b"\n") + 1
            self._offsets[path] = start + end
            for line in data[:end].splitlines():
                self._ingest(line)

    def _ingest(self, line: bytes):
        if b'"usage"' not in line:
            return
        try:
            d = json.loads(line)
        except ValueError:
            return
        if d.get("type") != "assistant":
            return
        msg = d.get("message")
        if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
            return
        # The same response can be written more than once while it streams.
        key = f"{msg.get('id')}:{d.get('requestId')}"
        if key in self._seen:
            return
        self._seen.add(key)
        try:
            ts = dt.datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00"))
        except (KeyError, ValueError):
            return
        u = msg["usage"]
        self._entries.append((
            ts, _model_family(msg.get("model")),
            int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
            int(u.get("cache_creation_input_tokens") or 0), int(u.get("cache_read_input_tokens") or 0),
        ))

    def _current_block(self, now: dt.datetime):
        """Usage windows start at the hour of the first message after the previous
        window ended, and last five hours."""
        start = None
        for e in self._entries:
            if start is None or e[0] >= start + BLOCK:
                start = e[0].replace(minute=0, second=0, microsecond=0)
        if start is None or now >= start + BLOCK:
            return None
        return start

    def _limits(self, last_active) -> dict:
        """Status line readings, or live ones when enabled and newer."""
        limits = read_limits() if self.bridge else {}
        if self.live is not None:
            recent = (last_active is not None and
                      dt.datetime.now(dt.timezone.utc) - last_active < dt.timedelta(minutes=15))
            live = self.live.get(active=recent)
            if live and live.get("updated", 0) >= limits.get("updated", 0):
                limits = live
        return limits

    def snapshot(self) -> dict:
        self._scan()
        now = dt.datetime.now(dt.timezone.utc)
        horizon = now - dt.timedelta(days=HISTORY_DAYS)
        self._entries = sorted(e for e in self._entries if e[0] >= horizon)

        local_today = dt.datetime.now().astimezone().date()
        days = [local_today - dt.timedelta(days=i) for i in range(HISTORY_DAYS - 1, -1, -1)]
        per_day = {d: 0 for d in days}
        today = {"tokens": 0, "output": 0, "messages": 0}
        models: dict[str, int] = {}
        last_hour = 0
        for ts, fam, inp, out, cc, cr in self._entries:
            total = inp + out + cc + cr
            day = ts.astimezone().date()
            if day in per_day:
                per_day[day] += total
            if day == local_today:
                today["tokens"] += total
                today["output"] += out
                today["messages"] += 1
                models[fam] = models.get(fam, 0) + total
            if now - ts <= dt.timedelta(hours=1):
                last_hour += total

        block = {"active": False}
        start = self._current_block(now)
        if start is not None:
            tokens = msgs = 0
            for ts, _, inp, out, cc, cr in self._entries:
                if ts >= start:
                    tokens += inp + out + cc + cr
                    msgs += 1
            block = {
                "active": True,
                "start": int(start.timestamp()),
                "reset": int((start + BLOCK).timestamp()),
                "tokens": tokens,
                "messages": msgs,
            }

        last = self._entries[-1][0] if self._entries else None
        return {
            "limits": self._limits(last),
            "type": "claude",
            "today": today,
            "block": block,
            "last_hour": last_hour,
            "days": [per_day[d] for d in days],
            "models": [{"name": k, "tokens": v}
                       for k, v in sorted(models.items(), key=lambda kv: -kv[1]) if v > 0][:4],
            "last_active": int(last.timestamp()) if last else 0,
        }
