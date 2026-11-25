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
workbench converts in a gate of its own. From workbench 0.10.0 a measure has a
gate of its own too, so Send never waits for an Office file or a measure while
workbench's `GHOSTWRITER_WORKBENCH_SLOTS` is 4 or more, its default; at 3, a
prepare, a conversion and a measure together take every slot. A video or audio
clip is taken too, once Nextext is set and workbench is 0.9.0 or later: it goes
to Nextext for one job, and its transcript and frames come back into workbench
as derived files of the clip, which the model reads with `read_document` and
looks at with `view_file`.

What limits an answer is told to the user, never left to be guessed (design:
`deploy/docs/superpowers/specs/2026-11-24-ghostwriter-limit-notices-design.md`).
An answer carries notices, stored with it: `answer_cut` (it ended at its token
cap, and the latest such answer offers Continue), `document_partial` and
`document_unread` (a file was read only in part, or an attachment of the
question not at all), `images_dropped` and `context_trimmed`; an answer the
router refuses for the model's context ends in the `context` error. A document
workbench has measured (0.10.0) is known to be longer than one prompt, so its
tile warns and the model is told to page. The composer shows how full the
context is, a ring, from the estimate stored with the last answer. Reference:
`docs/`.

## Where it fits

| | backend | frontend |
|---|---|---|
| Networks (alias) | `inference-net` and `workbench-net` (`ghostwriter-backend`); `ghostwriter-net` (`backend`) | `edge-net` (`ghostwriter-frontend`); `ghostwriter-net` (`frontend`) |
| Port | 8000: `/api/…`, `/metrics` | 8080: nginx |
| Talks to | the router `vllm-router:4000`; workbench `workbench-api:8000`, caller `ghostwriter`; Nextext `nextext-backend:8000` on `inference-net`, service `service:ghostwriter` | the backend only |

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
  restarts or the file is deleted. From 0.8.1 too, an attachment not yet sent is
  taken back with the X on its tile: `DELETE /files/upload` deletes its file and
  renders there, and then its row goes (`api/files.py:remove_attachment`).
  workbench 0.10.0 measures a document (`POST /files/measure`, body `{path}`;
  `WorkbenchClient.measure`; the answer holds `unit`, `total`, `chars` and
  `capped`) on a measure gate of its own, and raises its
  `GHOSTWRITER_WORKBENCH_SLOTS` to 4. Every `read_document` part from 0.10.0
  carries `units` (`[first, last]`, 1-based, or `null`) and `cut`. Ghostwriter
  measures each `document` attachment in the background after its upload, once
  discovery names 0.10.0 or later (`serves_measure`): on `MEASURE_SLOTS` (1) of
  its own, never a prepare slot, never `BACKGROUND_SLOTS`, with the wait of a
  prepare (`PREPARE_TIMEOUT_S`, 390 s, above the 270 s of workbench's measure
  gate at its defaults). Its state (`attachments.measure_state`: `pending`,
  `measured`, `none`) never holds Send back. Measured, a document is `too_long`
  when its count was capped or its `chars` over `CHARS_PER_TOKEN` pass the prompt
  budget: the tile warns, its attachment line adds `LONGER_THAN_A_PROMPT`, and
  the system prompt, with tools, adds `PAGING`. What the model read of a file
  comes from the `units` of the parts it was given, never from a part cut here.
- **The Nextext seam and clips.** Nextext is reached at `NEXTEXT_API_BASE` on
  `inference-net` with Ghostwriter's own service token (`NEXTEXT_API_KEY`, the
  same value as its entry `ghostwriter=<token>` in Nextext's
  `NEXTEXT_SERVICE_TOKENS`; `make env` writes both), the purpose and the request
  id. Clips are offered (`NextextHealth.offered`, `/api/config`'s `media`) only
  while the base and the key are set, a background probe of `/ready` passes
  (Nextext 1.22: it can transcribe, `/health` stays up while its Whisper is
  down; a Nextext before 1.22 answers 404 and `/health` is asked instead;
  after Nextext refused the key, which logs one error naming
  `NEXTEXT_API_KEY`, also a keyed `GET /jobs`) and workbench's discovery names
  0.9.0 or later; a job that fails `inference_unavailable` turns clips off at
  once, until the probe passes again; the start-up audit event's `media`
  criterion means only "configured". One clip is one Nextext job: streamed from
  workbench's `GET /files/view` as `media.<ext>`, polled through `GET /jobs`
  (never `GET /jobs/{id}`, whose answer holds the transcript), its `docint.jsonl`
  and `keyframes.zip` streamed into workbench's `POST /files/derived`, the job
  deleted at once in a shielded `finally`. workbench 0.9 settles the clip's
  manifest (kind `media`, unit `frame`, `duration_s`, `transcript`) and leaves
  the clip out of `run_python` and `search_files`, so the model reaches it only
  through `read_document` and `view_file`. A job no clip owns is deleted by the
  reconcile: at start, after a create whose answer was lost or a clip's own
  delete that failed, and in the daily sweep. A clip taken back, or whose chat is deleted, mid-job still has its
  Nextext job deleted, but Nextext's pipeline runs on until it ends. A video
  with no sound track fails (`undecodable_media`) only at a Nextext before 1.22;
  from 1.22 it is skipped (`no_audio_stream`) and the clip is `ready` with its
  frames and no transcript. Reference:
  `docs/data-flow.md`.

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
| `ghostwriter/store.py` | `ChatStore`: every read and write, by owner; the content deadlines (an answer's notices and a document's measure too); `too_long` |
| `ghostwriter/turns.py` | `TurnRegistry`: at most one answer per chat |
| `ghostwriter/sse.py` | SSE frames; docint's sync-generator bridge |
| `ghostwriter/body_limit.py` | per-route body caps, as nginx's |
| `ghostwriter/upload.py` | streaming multipart reader; attachment types, kinds and safe names |
| `ghostwriter/inference.py` | `ModelClient`: chat rounds and transcriptions through the router |
| `ghostwriter/workbench.py` | `WorkbenchClient`: MCP tools, upload, view, prepare, `measure` (a document's length, 0.10.0), renditions, `derived` (a clip's artifacts in), chat delete; the pseudonyms |
| `ghostwriter/vision.py` | `Vision.images_offered` (setting, model, `view_file` listed); the Ollama probe; `system_call` |
| `ghostwriter/nextext.py` | `NextextClient`: create (a streamed multipart), poll through the list, artifact (streamed, capped), delete, list; `NextextHealth`: the `/ready` probe (`/health` for a Nextext before 1.22) and the offer (`offered`, which also needs workbench 0.9); `JOB_OPTIONS` |
| `ghostwriter/processing.py` | `AttachmentProcessor`: prepares a picture or a PDF after its upload (`processing` → `ready` / `failed`), and an Office file after it is `ready`, in the background, one at a time (`BACKGROUND_SLOTS`); the measure step (a document's length, last in its job, one at a time on `MEASURE_SLOTS`, `pending` → `measured` / `none`, recorded as `workbench:measure`); the media step of a clip (to Nextext and back on `MEDIA_SLOTS`, then the prepare), the owned job ids, and `reconcile` (Nextext's jobs no clip owns: at start, after a lost create or a failed delete, in the sweep); the long-poll's `wait`, requeue at start and after a failed delete, cancel on a chat's delete or an attachment's take-back |
| `ghostwriter/retention_rule.py` | the period rule (docint's, verbatim) and the sweep loop |
| `ghostwriter/retention.py` | the daily sweep (its first step is the Nextext reconcile, bounded, never stopping the chats) and the dry run |
| `ghostwriter/log_cfg.py` | the stderr sink; quiet HTTP and SDK loggers |
| `ghostwriter/agent/limits.py` | the caps per message |
| `ghostwriter/agent/loop.py` | `AgentLoop`: rounds and tool calls within the caps; the notices it tells, `_refuses_context` (the `context` error), the estimate stored with every answer (`TurnOutcome`), and Continue (`Turn.continued`: one round, no tools, no thinking, appended in place) |
| `ghostwriter/agent/notices.py` | `NoticeEvent` (the live frame with a file's name, the stored form without it), `Reading` (what a message's calls opened and read: `document_partial`, `document_unread`), `SentFile`, `covered` (the union of the units read) |
| `ghostwriter/agent/stream.py`, `reasoning.py` | one streamed round; reasoning stripped (docint's) |
| `ghostwriter/agent/context.py` | the prompt fitted to the budget; follow-ups replayed; `FitReport` (the earlier turns there were and those replayed, from the first round); `next_prompt_tokens` and `finished_turn` (the estimate of the next prompt, counted as `fit` sizes); `continuation` (a continued turn's messages); `LONGER_THAN_A_PROMPT` in a document's line |
| `ghostwriter/agent/images.py` | image refs, their token estimate, the notes where one gives way, the `ImageBank` and `materialise` (refs to data URLs, as a round goes out) |
| `ghostwriter/agent/tools.py` | tools as OpenAI functions; arguments read; what the model reads back |
| `ghostwriter/agent/prompt.py` | the system prompt; `PAGING` (read a long document in parts, say which); `CONTINUE` (the instruction for a provider that does not go on in the answer's own message) |
| `ghostwriter/api/chats.py` | list, create, read, rename, delete |
| `ghostwriter/api/messages.py` | send, regenerate and continue (SSE), stop |
| `ghostwriter/api/files.py` | attachments and downloads |
| `ghostwriter/api/speech.py` | `/api/transcribe`, only with speech input on |
| `ghostwriter/api/meta.py` | `/api/whoami`, `/api/config` (with the prompt budgets, plain and reasoning) |
| `frontend/src/api/` | `apiFetch` (the purpose on every request), routes, SSE reader |
| `frontend/src/hooks/` | chats, the chat streams (one per chat, held above the open chat), attachments, the recorder, the stray-drop guard |
| `frontend/src/hooks/usePreview.ts` | a picture's render as an object URL, fetched once with the purpose; says when it is gone |
| `frontend/src/components/` | chat list, conversation, trail, result cards, composer, retention note |
| `frontend/src/components/AttachmentTiles.tsx` | the composer's tiles: uploading, processing (a clip: transcribing), ready, failed; preview and lightbox (a clip has neither: its icon only) |
| `frontend/src/components/AttachmentImage.tsx` | a sent picture in the user's bubble, opening full size in a lightbox |
| `frontend/src/components/NoticeLines.tsx` | an answer's notices as lines under it, Continue on the latest `answer_cut` |
| `frontend/src/components/ContextNote.tsx`, `hooks/useContextUse.ts`, `lib/context.ts` | the composer's note (nearly full, trimmed; a New chat button), the ring's ratio from the last answer's estimate, `NEAR_FULL_RATIO` |
| `frontend/src/i18n/` | en and de with key parity; German uses "Sie" |
| `frontend/nginx/` | sub-path, the stream, body caps, security headers |

## Invariants

- No accounts or passwords. Identity is `X-Auth-User`, trusted only with
  `GHOSTWRITER_EDGE_SECRET` once that is set.
- No user sees another user's chat: no admin path, `owner` parameter or group check.
- No file bytes here: no spool to disk, no copy in the database. Uploads and
  downloads stream through.
- A clip streams to Nextext for one job, the one copy of a file outside workbench
  (ADR 0009); every other rule stands: no spool, nothing under the user's file
  name (`media.<ext>`), the job deleted at once, no content in a log or an event.
- A file is reachable only through its own chat: every lookup is by
  `(owner, chat_id, file_id)`, and every workbench call carries that chat's `X-Session-Id`.
- Code runs only in workbench.
- Inference goes only through `OPENAI_API_BASE`, with `X-Cache-Scope` and `X-Request-ID`.
- Nothing is fetched at runtime: no CDN; the highlighting languages are bundled.
- Audit records references only; an action whose event cannot be written does
  not happen (fail closed).
- Logs hold ids, counts, statuses and tokens, never content, file names or a
  router's refusal text (loguru `{}` placeholders).
- A notice holds a code, a file's id and counts, never content and never a
  file's name (the name is read from the attachment row when it is served); a
  measure sends a path and keeps four counts.

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
   own results (`agent/context.py` docstring). An attached clip was processed
   before the message could name it (the media step of `processing.py`: Nextext,
   then workbench); its line reads `video 02:14, 8 frames; transcript`, and the
   model reads the transcript with `read_document` and looks at the frames with
   `view_file`, the only way to its frames, so with vision only. A tool's
   images follow its tool messages in one user message. The renditions
   are fetched into the message's `ImageBank` (memory, for one message), and
   only `materialise`, just before the model call, swaps each ref for its label
   and a data URL, in a new list. A round the router refuses with images before
   any text goes once more without them, and the turn goes on as without sight.
   The message's first round fills a `FitReport` (the earlier turns there were
   and those replayed; filled again for the try without images): a request the
   router took that left earlier turns out tells `context_trimmed`, once, before
   any text. A refusal for the model's context (`_refuses_context`: a 400 or 413
   naming the context) goes the same way once without images, and ends the turn
   with the `context` error when none were sent or it is refused again; the log
   then names `GHOSTWRITER_CONTEXT_TOKENS`, which may be above the model's real
   context. A tool result goes to the model cut at `TOOL_RESULT_CHARS` (16,896:
   workbench's 16 KB default plus 512 for the fields its size check leaves out,
   so that a default answer is never cut), and a `read_document` answer is cut
   by whole parts, keeping `next_part`.
5. The frames are `start` (always first), then `token`, `step`, `result`,
   `notice` and `error`, then `final` (always last), with a keep-alive comment
   every 15 s. A `notice` is a code with every detail it has, a file's name
   included (`file_id`, `name`, `read`, `total`, `unit`; stored without the
   name, which is read from the attachment when the answer is served):
   `context_trimmed` before any text; `images_dropped` where it happens;
   `answer_cut` after the text, when the last round, one without calls, ended
   at its token cap; then, once the rounds have ended (a stop or a model error
   too), `document_partial` for each attachment of the chat that
   `read_document` read only in part, and `document_unread` for each
   attachment of the question no call opened, none of either after a
   `run_python` or `search_files` call (they read files without naming them);
   an `error` is last. `final` carries `context_estimate`, the estimated tokens
   of the next prompt, as stored with the answer (`null` for a turn stopped
   before it began). A stop or a disconnect sets the turn's event: the call in
   flight finishes and is stored, and no further round starts.
6. Every `ChatStore` method is one short transaction that returns schema
   objects. File marks set how long content derived from a file may live
   (`store.py` docstring); the daily sweep (`retention.py`) deletes by the same
   deadlines. The answer is stored with its notices (`messages.notices`; the
   details of a file's notice, its id and counts, follow the content deadline
   through `notices_expire_at`, then only the code stays) and the estimate of the
   next prompt (`messages.context_estimate`): the system prompt, the tool schemas
   and every turn as `fit` would replay it, this answer among them, counted by
   the same code and with `CHARS_PER_TOKEN`, so a chat past its budget shows as
   past it. `/api/config` serves the budgets it is read against, `prompt_tokens`
   and `reasoning_prompt_tokens` (`limits.prompt_tokens` of
   `GHOSTWRITER_CONTEXT_TOKENS`); the SPA divides the estimate by the one the
   reasoning switch selects, for the ring, and decides "nearly full"
   (`NEAR_FULL_RATIO`, 0.8, a frontend constant) on the same unrounded number.

**Continue.** `POST /api/chats/{id}/messages/{message_id}/continue` goes on with
the chat's latest answer while it carries `answer_cut`; any other answer, a
question or an unknown id is 409 `not_continuable`, and a chat with a turn
running 409 `busy`, both before anything is recorded or stored. It claims the
chat as a message does and records `modify` / `chat.continue` (target the
answer, one `chat.answer` child for its one round) before anything is stored.
`Turn.continued` runs one round with no tools and no thinking, on the budget of
a round without thinking, over the earlier turns, the turn replayed as history
and the answer so far as the last assistant message (`context.continuation`).
On vllm the body is `CONTINUE_BODY` (`continue_final_message: true`,
`add_generation_prompt: false`, `enable_thinking: false`), so the template goes
on in that message; any other provider gets the answer so far and then the user
instruction `CONTINUE`, a less seamless join. The new text is appended to the
same answer, with the same frames (`start` and `final` name the answer and no
question); its other notices stay, and `answer_cut` is computed again: gone once
the round ended cleanly below the cap, told again after a cut, an error or a
stop. The estimate is made again. Regenerate replaces the whole answer after it,
as before.

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
- The media suffix table, `upload.MEDIA_TYPES` (suffixes and types), and
  workbench's `renders.MEDIA_TYPES`. Which are audio: `upload.AUDIO_SUFFIXES` and
  `AUDIO_EXTENSIONS` in `frontend/src/lib/format.ts`.
- `JOB_OPTIONS["keyframes_max"]` and `["keyframes_per_minute"]` in `nextext.py`,
  and workbench's `MEDIA_FRAMES_MAX` (19): Nextext's frame count is the least of
  the first, minutes times the second, and the container's keyframes; its own
  default of 4 a minute gives a 30 s clip 2 frames.
- `tests/test_scaffold.py` checks the text of `docker/compose.yaml`, the
  `Makefile`, nginx, Vite's `base` and `.env.example` (its tag comments,
  lines under 80 characters).
- A setting: `load_settings` in `config.py`, `.env.example`, `docs/configuration.md`.
- The notice codes: `schemas.NoticeCode` (the backend's `notices.NOTICE_CODES`
  follows it), `NoticeCode` in `frontend/src/api/types.ts`, `NOTICE_CODES` in
  `frontend/src/hooks/chatStreams.tsx`, and the `notice.*` strings and the lines
  of `NoticeLines.tsx`; a stored code this version does not know reads as none.
- The measure units: `schemas.MeasureUnit`, `_MEASURE_UNITS` in `workbench.py`
  and workbench's own units, `MeasureUnit` in `frontend/src/api/types.ts`,
  `UNIT_KEYS` in `NoticeLines.tsx` and the `unit.*` strings (many and one).
- `CHARS_PER_TOKEN` and `limits.prompt_tokens`: they size the prompt budget,
  the `too_long` of a measured document (`store.py`), the next-prompt estimate
  and the budgets `/api/config` serves, which the ring and "nearly full" divide
  by (`NEAR_FULL_RATIO` and `lib/context.ts`).
- The schema (now version 3: attachments carry `kind`, `status`, `render`,
  `failure`, `measure`, `measure_state`; answers `notices`,
  `context_estimate`, `notices_expire_at`): raise `SCHEMA_VERSION` in `db.py` and add
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
