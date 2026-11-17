# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Data confidentiality — hard rule

**NEVER expose actual production or testing data in any file committed or
pushed to git.** This covers not only file contents but also metadata that
references real data: filenames, file descriptions, social-media account
names or handles, user identifications, sample records, log excerpts, and
screenshots. It applies everywhere git sees — source code, tests, fixtures,
docs, examples, configs, commit messages, and CI files. Use fully synthetic,
invented placeholders instead.

**Likewise, NEVER expose local filepaths from development machines** —
absolute paths or home directories such as `/Users/<name>/...`,
`/home/<name>/...`, or `C:\Users\...` — anywhere git sees. The only
permitted paths are relative project paths starting from the project's
root (e.g. `docker/compose.yaml`).

## What this repo is

Ghostwriter is the federation's chat assistant first: plain conversation is a
full use of it, also with workbench off or unreachable (no tool is offered, and
the composer hides the paperclip). Files are what it can also do: an attached
document goes to workbench, which runs the tools over it and hands the results
back as cards; no file is stored here. A FastAPI backend keeps the chats in
SQLite; a React SPA (`@infra/ui`, nginx) is reached only through the edge
gateway. Reasoning is a per-message switch in the composer, off by default; on
vllm it sets `chat_template_kwargs.enable_thinking`, and the thinking is
dropped (never shown, stored, replayed or recorded). Reference: `docs/`.

## Where it fits

| | backend | frontend |
|---|---|---|
| Networks (alias) | `inference-net` and `workbench-net` (`ghostwriter-backend`); `ghostwriter-net` (`backend`) | `edge-net` (`ghostwriter-frontend`); `ghostwriter-net` (`frontend`) |
| Port | 8000: `/api/…`, `/metrics` | 8080: nginx |
| Talks to | the router `vllm-router:4000`; workbench `workbench-api:8000`, caller `ghostwriter` | the backend only |

- **Volume:** the external `ghostwriter-data` at `/var/lib/ghostwriter`
  (`ghostwriter.sqlite3`). `docker compose down -v` leaves it; only `make nuke`
  removes it.
- **Edge route:** `/ghostwriter/` on `:443`. nginx strips the prefix; Vite's
  `base` is `/ghostwriter/`.
- **obs-plane** collects the audit chain from stdout and scrapes
  `ghostwriter-backend:8000/metrics`.

## Module map

| Module | What |
|---|---|
| `ghostwriter/main.py` | `create_app`: lifespan (audit chain, store, agent, sweep), error handlers, routers |
| `ghostwriter/config.py` | `Settings`, read once at start; a value that cannot work stops startup |
| `ghostwriter/identity.py` | `X-Auth-User`, trusted only with `X-Edge-Secret`; the dev identity |
| `ghostwriter/audit.py` | `Operation`, `audited`, the purpose rule, `forward_headers` |
| `ghostwriter/utils/audit_*.py` | vendored from the hub's `configs/audit`; never edited |
| `ghostwriter/db.py`, `models.py` | SQLite engine and pragmas, schema version, the tables |
| `ghostwriter/schemas.py` | what the API serves; `expired` is computed at read time |
| `ghostwriter/store.py` | `ChatStore`: every read and write, by owner; the content deadlines |
| `ghostwriter/turns.py` | `TurnRegistry`: at most one answer per chat |
| `ghostwriter/sse.py` | SSE frames; docint's sync-generator bridge |
| `ghostwriter/body_limit.py` | per-route body caps, as nginx's |
| `ghostwriter/upload.py` | streaming multipart reader; attachment types and safe names |
| `ghostwriter/inference.py` | `ModelClient`: chat rounds and transcriptions through the router |
| `ghostwriter/workbench.py` | `WorkbenchClient`: MCP tools, upload, view, chat delete; the pseudonyms |
| `ghostwriter/retention_rule.py` | the period rule (docint's, verbatim) and the sweep loop |
| `ghostwriter/retention.py` | the daily sweep and the dry run |
| `ghostwriter/log_cfg.py` | the stderr sink; quiet HTTP and SDK loggers |
| `ghostwriter/agent/limits.py` | the caps per message |
| `ghostwriter/agent/loop.py` | `AgentLoop`: rounds and tool calls within the caps |
| `ghostwriter/agent/stream.py`, `reasoning.py` | one streamed round; reasoning stripped (docint's) |
| `ghostwriter/agent/context.py` | the prompt fitted to the budget; follow-ups replayed |
| `ghostwriter/agent/tools.py` | tools as OpenAI functions; arguments read; what the model reads back |
| `ghostwriter/agent/prompt.py` | the system prompt |
| `ghostwriter/api/chats.py` | list, create, read, rename, delete |
| `ghostwriter/api/messages.py` | send and regenerate (SSE), stop |
| `ghostwriter/api/files.py` | attachments and downloads |
| `ghostwriter/api/speech.py` | `/api/transcribe`, only with speech input on |
| `ghostwriter/api/meta.py` | `/api/whoami`, `/api/config` |
| `frontend/src/api/` | `apiFetch` (the purpose on every request), routes, SSE reader |
| `frontend/src/hooks/` | chats, the chat streams (one per chat, held above the open chat), attachments, the recorder, the stray-drop guard |
| `frontend/src/components/` | chat list, conversation, trail, result cards, composer, banner |
| `frontend/src/i18n/` | en and de with key parity; German uses "Sie" |
| `frontend/nginx/` | sub-path, the stream, body caps, security headers |

## Invariants

- No accounts or passwords. Identity is `X-Auth-User`, trusted only with
  `GHOSTWRITER_EDGE_SECRET` once that is set.
- No user sees another user's chat: no admin path, `owner` parameter or group check.
- No file bytes here: no spool to disk, no copy in the database. Uploads and
  downloads stream through.
- A file is reachable only through its own chat: every lookup is by
  `(owner, chat_id, file_id)`, and every workbench call carries that chat's `X-Session-Id`.
- Code runs only in workbench.
- Inference goes only through `OPENAI_API_BASE`, with `X-Cache-Scope` and `X-Request-ID`.
- Nothing is fetched at runtime: no CDN; the highlighting languages are bundled.
- Audit records references only; an action whose event cannot be written does
  not happen (fail closed).
- Logs hold ids, counts, statuses and tokens, never content or file names
  (loguru `{}` placeholders).

**One process.** The backend runs as `uvicorn --factory ghostwriter.main:create_app`
with one worker: the process holds the one audit chain, the turn registry and
the sweep. Never add workers or replicas.

## Commands

```bash
make verify                       # pre-push gate: pre-commit (ruff, pyrefly) + frontend lint and build
make test                         # pytest, then vitest
make retention-dry-run CHAT_DAYS=60   # in the running backend: what retention would delete; changes nothing
make nuke                         # DESTROY ghostwriter-data, every chat (type 'ghostwriter')
```
