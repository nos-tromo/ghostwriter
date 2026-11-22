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
dropped (never shown, stored, replayed or recorded). A picture attached to a
message, or a page or picture a tool shows, goes to the chat model as an image
while it sees (`GHOSTWRITER_VISION`); the bytes live in memory for that one
message. An attached Word, PowerPoint or OpenDocument text file is prepared too,
so its pages and slides can be seen (workbench 0.8.1): it is `ready` at once and
prepared in the background, so Send never waits for it. That holds because
workbench converts in a gate of its own, with `GHOSTWRITER_WORKBENCH_SLOTS` at 3
or more (its default). Reference: `docs/`.

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
- **The workbench seam and pictures.** workbench 0.7.0 lists `view_file`, tells
  `read_document` in its image words and attaches images only to a call that
  carries `X-Accept-Images: 1` (`workbench.ACCEPT_IMAGES`); without it, it
  answers as 0.6.0 did. Discovery is cached per listing (`discover(images=…)`).
  A turn sees when the model sees and the image listing holds `view_file`: it is
  offered that listing, and its calls carry the header. A turn without sight,
  and every round after the router refused a round's images, is offered the
  header-less listing and sends no header. Pictures are offered
  (`Vision.images_offered`) on the same condition. On Ollama, one
  `POST /api/show` to the router's host at start turns pictures off for a model
  without `vision`; on vLLM, a text-only model needs `GHOSTWRITER_VISION=off`.
  workbench 0.8.0 converts an Office file once and keeps the PDF as a derived
  file; next to 0.7.0 its prepare answers kind `none` and the file reads as text.
  From 0.8.1 it converts one file at a time in a gate apart from the pictures' and PDFs'
  prepares; a tool call for a deck's slides waits out the deck's background
  prepare, up to 120 s (within `WORKBENCH_TIMEOUT`, 150 s); and a file it refuses
  as unreadable or unsupported is refused again at once on later views, until it
  restarts or the file is deleted.

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
| `ghostwriter/upload.py` | streaming multipart reader; attachment types, kinds and safe names |
| `ghostwriter/inference.py` | `ModelClient`: chat rounds and transcriptions through the router |
| `ghostwriter/workbench.py` | `WorkbenchClient`: MCP tools, upload, view, prepare, renditions, chat delete; the pseudonyms |
| `ghostwriter/vision.py` | `Vision.images_offered` (setting, model, `view_file` listed); the Ollama probe; `system_call` |
| `ghostwriter/processing.py` | `AttachmentProcessor`: prepares a picture or a PDF after its upload (`processing` → `ready` / `failed`), and an Office file after it is `ready`, in the background, one at a time (`BACKGROUND_SLOTS`); the long-poll's `wait`, requeue at start and after a failed delete, cancel on a chat's delete |
| `ghostwriter/retention_rule.py` | the period rule (docint's, verbatim) and the sweep loop |
| `ghostwriter/retention.py` | the daily sweep and the dry run |
| `ghostwriter/log_cfg.py` | the stderr sink; quiet HTTP and SDK loggers |
| `ghostwriter/agent/limits.py` | the caps per message |
| `ghostwriter/agent/loop.py` | `AgentLoop`: rounds and tool calls within the caps |
| `ghostwriter/agent/stream.py`, `reasoning.py` | one streamed round; reasoning stripped (docint's) |
| `ghostwriter/agent/context.py` | the prompt fitted to the budget; follow-ups replayed |
| `ghostwriter/agent/images.py` | image refs, their token estimate, the notes where one gives way, the `ImageBank` and `materialise` (refs to data URLs, as a round goes out) |
| `ghostwriter/agent/tools.py` | tools as OpenAI functions; arguments read; what the model reads back |
| `ghostwriter/agent/prompt.py` | the system prompt |
| `ghostwriter/api/chats.py` | list, create, read, rename, delete |
| `ghostwriter/api/messages.py` | send and regenerate (SSE), stop |
| `ghostwriter/api/files.py` | attachments and downloads |
| `ghostwriter/api/speech.py` | `/api/transcribe`, only with speech input on |
| `ghostwriter/api/meta.py` | `/api/whoami`, `/api/config` |
| `frontend/src/api/` | `apiFetch` (the purpose on every request), routes, SSE reader |
| `frontend/src/hooks/` | chats, the chat streams (one per chat, held above the open chat), attachments, the recorder, the stray-drop guard |
| `frontend/src/hooks/usePreview.ts` | a picture's render as an object URL, fetched once with the purpose; says when it is gone |
| `frontend/src/components/` | chat list, conversation, trail, result cards, composer, retention note |
| `frontend/src/components/AttachmentTiles.tsx` | the composer's tiles: uploading, processing, ready, failed; preview and lightbox |
| `frontend/src/components/AttachmentImage.tsx` | a sent picture in the user's bubble, opening full size in a lightbox |
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

## A message, end to end

1. `api/messages.py` resolves the owner (`identity.py`) and writes the primary
   audit event (`collect` / `chat.message`, or `modify` / `chat.regenerate`)
   before anything is stored. A failed write raises `AuditWriteError`, which
   `main.py` answers with the generic 500.
2. `TurnRegistry.begin` claims the chat. A delete holds the same claim.
3. `AgentLoop` is a blocking generator. It runs on `app.state.turn_threads`
   (`TURN_THREADS`), not the shared pool, and `sse.aiter_sync_gen` bridges it to
   the event loop.
4. Each round: `context.fit` builds the messages within the prompt budget,
   replaying earlier turns newest first. `agent/stream.py` streams the round
   through `ModelClient` and strips the reasoning. Each tool call is disclosed
   (`disclose` / `chat.tool`) before it goes to `WorkbenchClient.call_tool`
   over MCP, which never raises. Each round is then recorded (`ai_call` /
   `chat.answer`, with how many images it sent). With workbench off,
   `app.state.workbench` is `None` and no tool is offered. While the turn sees,
   the question carries its attached pictures and a follow-up those of earlier
   questions that fit, as image parts: refs, never bytes. `fit` counts a ref by
   `estimate_image_tokens` and puts a note in its place where it gives way: the
   question's pictures, then the newest tool images, before the latest round's
   own results (`agent/context.py` docstring). A tool's images follow its tool
   messages in one user message. The renditions
   are fetched into the message's `ImageBank` (memory, for one message), and
   only `materialise`, just before the model call, swaps each ref for its label
   and a data URL, in a new list. A round the router refuses with images before
   any text goes once more without them, and the turn goes on as without sight.
5. The frames are `start` (always first), then `token`, `step`, `result`,
   `notice` (what the user is told while the answer goes on: `images_dropped`)
   and `error`, then `final` (always last), with a keep-alive comment every 15 s.
   A stop or a disconnect sets the turn's event: the call in flight finishes
   and is stored, and no further round starts.
6. Every `ChatStore` method is one short transaction that returns schema
   objects. File marks set how long content derived from a file may live
   (`store.py` docstring); the daily sweep (`retention.py`) deletes by the same
   deadlines.

## Commands

Python 3.11 only. Settings come from `.env` (`cp .env.example .env`, then set
`OPENAI_API_KEY`).

```bash
uv sync --locked                  # backend deps, dev group included
make network                      # once per host: inference-net, edge-net, workbench-net
make dev                          # build + up with host ports: SPA localhost:8504/ghostwriter/, backend :8004
cd frontend && pnpm dev           # Vite on :5173; /ghostwriter/api proxied to :8004 (GHOSTWRITER_BACKEND_ORIGIN)
make verify                       # pre-push gate: pre-commit (ruff, pyrefly) + frontend lint and build
make test                         # pytest, then vitest (make test-backend / make test-frontend for one)
uv run pytest tests/test_store.py -k <name> -q                 # one backend test
cd frontend && pnpm test src/components/Composer.test.tsx -t '<name>'   # one frontend test
make retention-dry-run CHAT_DAYS=60   # in the running backend: what retention would delete; changes nothing
make nuke                         # DESTROY ghostwriter-data, every chat (type 'ghostwriter')
```

In dev, the account is `dev`: `GHOSTWRITER_DEFAULT_IDENTITY`, which
`docker/compose.override.yaml` sets. It is ignored while
`GHOSTWRITER_EDGE_SECRET` is set, and every request then answers 401. Ruff
requires Google-style docstrings (`D`) and annotations (`ANN`); pyrefly runs
strict; lines are 120 characters.

## Testing

- Settings come from `make_settings(tmp_path, **overrides)` in
  `tests/conftest.py`, never from the environment. `as_user(name)` returns the
  gateway's headers.
- For the full app, build it as `_app` in `tests/test_api_messages.py` does:
  `create_app(settings, audit_stream=..., workbench_transport=httpx.MockTransport(FakeDoor()),
  model=ModelClient(settings, client=FakeOpenAI().as_client()))`. Script the
  model's rounds with `text_chunk`, `call_chunk`, `reasoning_chunk` and
  `usage_chunk` (`tests/fakes.py`).
- The `audit_stream` fixture checks the hash chain at teardown, so a test that
  breaks the chain fails even after its asserts pass. `loguru_caplog` captures
  loguru warnings in `caplog`.
- Every name, id and value in the fakes is invented; keep it that way.

## What changes together

- A body cap: `ghostwriter/body_limit.py` and `client_max_body_size` in
  `frontend/nginx/default.conf`.
- An image cap (`GHOSTWRITER_MAX_IMAGES`, `agent/images.py`, `vision.py`):
  vllm-service's `CHAT_LIMIT_MM_PER_PROMPT` must stay at or above it.
- An Office suffix: `upload.OFFICE_SUFFIXES`, `agent/images.strips_for_path`,
  `agent/context.attachment_facts` and workbench's `renders.OFFICE_SUFFIXES`.
- `tests/test_scaffold.py` checks the text of `docker/compose.yaml`, the
  `Makefile`, nginx, Vite's `base` and `.env.example` (its tag comments,
  lines under 80 characters).
- A setting: `load_settings` in `config.py`, `.env.example`, `docs/configuration.md`.
- The schema (now version 2: attachments carry `kind`, `status`, `render`,
  `failure`): raise `SCHEMA_VERSION` in `db.py` and add
  `MIGRATIONS[<previous version>]`, one step per version. A file from a newer
  version is refused. `retention.py`'s dry run reads the file directly, so it
  needs the same care for a column it selects.
- A new audit action goes into `ENDPOINT_ACTIONS` in `audit.py` first, or
  `audited()` raises. `docs/audit.md` lists what each route records.
- UI strings go into both `frontend/src/i18n/en.ts` and `de.ts`;
  `i18n.test.ts` checks that their keys match.
- These files are copied from `nos-tromo/.github`, and CI checks them against
  the originals, so change them there: `ghostwriter/utils/audit_*.py`,
  `make/common.mk`, `scripts/bundle-lib.sh`, `frontend/eslint.config.js`, and
  the `[tool.ruff]` / `[tool.pyrefly]` sections of `pyproject.toml` (only
  `target-version` may differ).
