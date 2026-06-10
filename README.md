# claude-agent-dashboard

A web "agent view" for local Claude Code sessions — the bird's-eye dashboard
that `claude agents` gives you in the terminal, but reachable from a browser
(e.g. your phone over Tailscale), **with the ability to reply to sessions**.

The UI mimics claude.ai/code: a sidebar lists sessions (status dot, title,
project, branch, age); clicking one opens the full conversation — user
messages, assistant replies, and collapsible tool calls / results / thinking
blocks — with a composer at the bottom to continue the session. Sessions are
deep-linkable via `http://host:8585/#<session-id>`.

Data comes from two places:

- **Transcripts** (`~/.claude/projects/*/*.jsonl`): conversation events, AI
  title, last prompt, git branch, permission mode, PR link.
- **Live session registry** (`~/.claude/sessions/<pid>.json`, filtered to
  processes that are still alive): whether a session is *attached* to a
  running CLI/daemon, its kind (`interactive` terminal vs `bg` worker), an
  authoritative status (`busy`/`waiting`/`idle`, incl. "waiting for
  permission prompt"), and — for remote-controlled sessions — the
  `bridgeSessionId` linking it to claude.ai/code.

Remote-control sessions get a 🌐 **remote** chip in the sidebar and an
"open in claude.ai/code" link in the header; other live sessions get a
**local** chip; unmarked sessions have no running process.

## Run

```bash
python3 dashboard.py                          # http://0.0.0.0:8585/
python3 dashboard.py --token SECRET           # require ?token=SECRET on every request
python3 dashboard.py --port 9000 --host 127.0.0.1
```

Stdlib only, no dependencies. The session list polls every 3 s and the open
conversation every 2.5 s; the server reads transcripts incrementally (only
bytes appended since the last poll) and serves the last 500 events per
session.

## Replying to a session

The composer under the conversation sends your message by running

```
claude -p --resume <session-id> "<your text>"
```

in the session's original cwd, detached. The same session ID keeps being
used (no fork), so the conversation continues with full context, the
transcript file grows, and the card flips to **working** until the turn
finishes. Output is also logged to `/tmp/claude-agent-dashboard/<id>.log`.

The permission-mode dropdown applies to the resumed headless run. In
`default` mode the agent cannot prompt you for tool permission — actions
outside the allowlist simply fail — so pick `acceptEdits` or
`bypassPermissions` when you expect it to do real work.

### Caveats

- Replies to a session **attached to a live process** are refused (HTTP
  409) — two writers on one transcript would conflict. Reply in the
  terminal, or via the session's claude.ai/code link if it has one. The
  composer is only enabled for detached sessions.
- Replies to a detached but **working** session are likewise refused.
- This is **remote code execution on your machine by design**. Keep it on
  localhost/Tailscale and use `--token` if anyone else can reach the port.

## Status inference

For attached sessions the registry status is authoritative:
`busy` → **working**, `waiting` → **needs input** (with the reason, e.g.
"permission prompt"). For detached sessions it falls back to transcript
heuristics:

- Last event = assistant finished its turn → **needs input**; after 4 h of
  silence shown as **idle**.
- Anything else → **working** while the transcript file is still growing;
  **idle** after 10 min of no growth.

Tune `NEEDS_INPUT_TTL` / `WORKING_TTL` at the top of `dashboard.py`.
