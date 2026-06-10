#!/usr/bin/env python3
"""Web dashboard for local Claude Code sessions ("agent view on the web").

Tails the JSONL transcripts under ~/.claude/projects/ and serves a small
status page. Stdlib only — run with:  python3 dashboard.py [--port 8585]
"""
import argparse
import collections
import json
import os
import re
import subprocess
import time
from glob import glob
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PROJECTS_DIR = Path.home() / ".claude" / "projects"
SESSIONS_DIR = Path.home() / ".claude" / "sessions"
HTML_PATH = Path(__file__).parent / "index.html"

# A session whose last event is "assistant finished its turn" is waiting on the
# user, but we can't tell a waiting session from a closed CLI — so after this
# many seconds of silence we call it idle either way.
NEEDS_INPUT_TTL = 4 * 3600
# If the model/tools were mid-flight but the file hasn't grown for this long,
# the session is no longer actively working.
WORKING_TTL = 10 * 60

# path -> {"offset": int, "size": int, "meta": dict}
_cache = {}

PERMISSION_MODES = {"default", "acceptEdits", "bypassPermissions", "plan"}
SESSION_ID_RE = re.compile(r"^[0-9a-f-]{36}$")
REPLY_LOG_DIR = Path("/tmp/claude-agent-dashboard")

# Conversation events for the chat view, parsed incrementally like _cache.
# path -> {"offset": int, "events": deque}
_msgs = {}
MAX_EVENTS = 500
MAX_TEXT = 4000


def _find_transcript(session_id):
    if not SESSION_ID_RE.match(session_id):
        return None
    hits = glob(str(PROJECTS_DIR / "*" / f"{session_id}.jsonl"))
    return hits[0] if hits else None


def _content_text(content):
    """Flatten a tool_result content field (str or list of blocks) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _line_events(d):
    """Convert one transcript line to zero or more chat-view events."""
    t = d.get("type")
    if d.get("isSidechain") or t not in ("user", "assistant"):
        return []
    out = []
    content = d.get("message", {}).get("content")
    if t == "user" and not d.get("isMeta"):
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for b in content or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and b.get("text", "").strip():
                txt = b["text"]
                # Skip harness-injected scaffolding, keep real prompts.
                if txt.lstrip().startswith(("<command-", "<local-command",
                                            "<system-reminder", "Caveat:")):
                    continue
                out.append({"kind": "user", "text": txt[:MAX_TEXT]})
            elif b.get("type") == "tool_result":
                out.append({"kind": "tool_result",
                            "text": _content_text(b.get("content"))[:1500],
                            "is_error": bool(b.get("is_error"))})
    elif t == "assistant":
        for b in content or []:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text" and b.get("text", "").strip():
                out.append({"kind": "assistant", "text": b["text"][:MAX_TEXT]})
            elif bt == "thinking" and b.get("thinking", "").strip():
                out.append({"kind": "thinking", "text": b["thinking"][:1500]})
            elif bt == "tool_use":
                inp = json.dumps(b.get("input", {}), ensure_ascii=False)
                out.append({"kind": "tool_use", "name": b.get("name", "?"),
                            "text": inp[:800]})
    return out


def read_messages(session_id):
    path = _find_transcript(session_id)
    if path is None:
        return None
    st = os.stat(path)
    entry = _msgs.get(path)
    if entry is None or st.st_size < entry["offset"]:
        entry = {"offset": 0, "events": collections.deque(maxlen=MAX_EVENTS)}
        _msgs[path] = entry
    if st.st_size > entry["offset"]:
        with open(path, "rb") as fh:
            fh.seek(entry["offset"])
            data = fh.read()
        end = data.rfind(b"\n") + 1
        for line in data[:end].splitlines():
            try:
                d = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            entry["events"].extend(_line_events(d))
        entry["offset"] += end
    return list(entry["events"])


def send_reply(session_id, text, permission_mode):
    """Continue a session headlessly via `claude -p --resume`. Returns error or None."""
    if not SESSION_ID_RE.match(session_id):
        return "bad session id"
    for path, entry in _cache.items():
        if Path(path).stem == session_id:
            meta, mtime = entry["meta"], os.stat(path).st_mtime
            break
    else:
        return "unknown session"
    reg = live_registry().get(session_id)
    if reg:
        where = "a terminal" if reg.get("kind") == "interactive" else "a background worker"
        hint = " — use its remote-control link" if reg.get("bridgeSessionId") else ""
        return f"session is attached to {where} (pid {reg['pid']}){hint}"
    if _status(meta, time.time() - mtime) == "working":
        return "session is actively working — replying now could fork it"
    cmd = ["claude", "-p", "--resume", session_id, text]
    if permission_mode != "default":
        cmd += ["--permission-mode", permission_mode]
    REPLY_LOG_DIR.mkdir(exist_ok=True)
    log = open(REPLY_LOG_DIR / f"{session_id}.log", "ab")
    subprocess.Popen(cmd, cwd=meta["cwd"] or str(Path.home()),
                     stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                     start_new_session=True)
    return None


def _new_meta():
    return {
        "title": None, "last_prompt": None, "away_summary": None,
        "pr_url": None, "pr_number": None, "permission_mode": None,
        "git_branch": None, "cwd": None, "last_event": None,
    }


def _apply_line(meta, line):
    try:
        d = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return
    t = d.get("type")
    if t == "ai-title":
        meta["title"] = d.get("aiTitle")
    elif t == "last-prompt":
        meta["last_prompt"] = d.get("lastPrompt")
    elif t == "pr-link":
        meta["pr_url"], meta["pr_number"] = d.get("prUrl"), d.get("prNumber")
    elif t == "permission-mode":
        meta["permission_mode"] = d.get("permissionMode")
    elif t == "system" and d.get("subtype") == "away_summary":
        meta["away_summary"] = d.get("content")
    elif t == "assistant" and not d.get("isSidechain"):
        meta["last_event"] = ("assistant", d.get("message", {}).get("stop_reason"))
    elif t == "user" and not d.get("isSidechain") and not d.get("isMeta"):
        meta["last_event"] = ("user", None)
    if d.get("gitBranch"):
        meta["git_branch"] = d["gitBranch"]
    if d.get("cwd"):
        meta["cwd"] = d["cwd"]


def _read_session(path):
    """Incrementally parse a transcript, returning its accumulated meta."""
    st = os.stat(path)
    entry = _cache.get(path)
    if entry is None or st.st_size < entry["size"]:  # new or truncated
        entry = {"offset": 0, "size": 0, "meta": _new_meta()}
        _cache[path] = entry
    if st.st_size > entry["offset"]:
        with open(path, "rb") as fh:
            fh.seek(entry["offset"])
            data = fh.read()
        # Don't consume a trailing partial line; re-read it next poll.
        end = data.rfind(b"\n") + 1
        for line in data[:end].splitlines():
            _apply_line(entry["meta"], line)
        entry["offset"] += end
    entry["size"] = st.st_size
    return entry["meta"], st.st_mtime


def live_registry():
    """sessionId -> ~/.claude/sessions/<pid>.json entry, live processes only."""
    reg = {}
    for path in glob(str(SESSIONS_DIR / "*.json")):
        try:
            with open(path) as fh:
                d = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        pid, sid = d.get("pid"), d.get("sessionId")
        if pid and sid and os.path.exists(f"/proc/{pid}"):
            reg[sid] = d
    return reg


def _status(meta, age):
    ev = meta["last_event"]
    if ev and ev[0] == "assistant" and ev[1] == "end_turn":
        return "needs-input" if age < NEEDS_INPUT_TTL else "idle"
    return "working" if age < WORKING_TTL else "idle"


def scan_sessions(max_age_hours):
    now = time.time()
    registry = live_registry()
    sessions = []
    for path in glob(str(PROJECTS_DIR / "*" / "*.jsonl")):
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            continue
        if now - mtime > max_age_hours * 3600:
            continue
        try:
            meta, mtime = _read_session(path)
        except OSError:
            continue
        age = now - mtime
        project_dir = Path(path).parent.name
        reg = registry.get(Path(path).stem)
        # A live CLI/daemon process reports authoritative status.
        if reg and reg.get("status") == "busy":
            status = "working"
        elif reg and reg.get("status") == "waiting":
            status = "needs-input"
        else:
            status = _status(meta, age)
        bridge = reg.get("bridgeSessionId") if reg else None
        sessions.append({
            "session_id": Path(path).stem,
            "project": meta["cwd"] or project_dir,
            "is_subagent": "-claude-worktrees-" in project_dir,
            "status": status,
            "attached": reg is not None,
            "kind": reg.get("kind") if reg else None,
            "waiting_for": reg.get("waitingFor") if reg else None,
            "remote_url": f"https://claude.ai/code/{bridge}" if bridge else None,
            "age_seconds": int(age),
            "title": meta["title"],
            "last_prompt": meta["last_prompt"],
            "away_summary": meta["away_summary"],
            "git_branch": meta["git_branch"],
            "permission_mode": meta["permission_mode"],
            "pr_url": meta["pr_url"],
            "pr_number": meta["pr_number"],
        })
    # Live sessions that haven't written a transcript yet (e.g. fresh
    # remote-control workers) only exist in the registry.
    seen = {s["session_id"] for s in sessions}
    for sid, reg in registry.items():
        if sid in seen:
            continue
        bridge = reg.get("bridgeSessionId")
        sessions.append({
            "session_id": sid,
            "project": reg.get("cwd"),
            "is_subagent": False,
            "status": {"busy": "working", "waiting": "needs-input"}.get(
                reg.get("status"), "idle"),
            "attached": True,
            "kind": reg.get("kind"),
            "waiting_for": reg.get("waitingFor"),
            "remote_url": f"https://claude.ai/code/{bridge}" if bridge else None,
            "age_seconds": max(0, int(time.time() - reg.get("updatedAt", 0) / 1000)),
            "title": reg.get("name") if reg.get("name") != sid[:8] else None,
            "last_prompt": None, "away_summary": None,
            "git_branch": None, "permission_mode": None,
            "pr_url": None, "pr_number": None,
        })
    sessions.sort(key=lambda s: s["age_seconds"])
    return sessions


class Handler(BaseHTTPRequestHandler):
    token = None  # set in main()

    def log_message(self, *args):
        pass

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self, url):
        if self.token is None:
            return True
        return parse_qs(url.query).get("token", [None])[0] == self.token

    def do_GET(self):
        url = urlparse(self.path)
        if not self._authorized(url):
            return self._send(403, "text/plain", b"missing or bad ?token=")
        if url.path == "/":
            self._send(200, "text/html; charset=utf-8", HTML_PATH.read_bytes())
        elif url.path == "/api/sessions":
            hours = float(parse_qs(url.query).get("hours", ["48"])[0])
            body = json.dumps(scan_sessions(hours)).encode()
            self._send(200, "application/json", body)
        elif url.path == "/api/messages":
            sid = parse_qs(url.query).get("session_id", [""])[0]
            events = read_messages(sid)
            if events is None:
                return self._send(404, "application/json", b'{"error": "unknown session"}')
            self._send(200, "application/json", json.dumps({"events": events}).encode())
        else:
            self._send(404, "text/plain", b"not found")

    def do_POST(self):
        url = urlparse(self.path)
        if not self._authorized(url):
            return self._send(403, "text/plain", b"missing or bad ?token=")
        if url.path != "/api/reply":
            return self._send(404, "text/plain", b"not found")
        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length))
            session_id = req["session_id"]
            text = req["text"].strip()
            mode = req.get("permission_mode", "default")
            assert text and mode in PERMISSION_MODES
        except (json.JSONDecodeError, KeyError, AssertionError, ValueError):
            return self._send(400, "application/json", b'{"error": "bad request"}')
        # Make sure the session cache is warm so send_reply can find it.
        scan_sessions(24 * 365)
        err = send_reply(session_id, text, mode)
        body = json.dumps({"error": err} if err else {"ok": True}).encode()
        self._send(409 if err else 202, "application/json", body)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8585)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--token", help="require ?token=... on every request "
                    "(recommended when binding beyond localhost)")
    args = ap.parse_args()
    Handler.token = args.token
    suffix = f"?token={args.token}" if args.token else ""
    print(f"Claude agent dashboard on http://{args.host}:{args.port}/{suffix}")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
