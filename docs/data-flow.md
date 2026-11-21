# Data flow

The record of processing: what Ghostwriter keeps, for how long, where data
goes, and who can read it.

## What is stored

SQLite on the `ghostwriter-data` volume (`ghostwriter/models.py`).

| Table | Columns | Kept |
|---|---|---|
| `chats` | id, owner (the account), title (the first message's first line, or the owner's), created_at, updated_at, expires_at | until `expires_at` (90 days after the start, [retention.md](retention.md)) or the owner's delete |
| `messages` | id, chat_id, position, role (`user`, `assistant`), content (the text sent, and the answer shown), created_at | with the chat |
| `tool_steps` | id, message_id, position, round, call_id, tool, arguments (the model's, code included), result (what the model read back, at most 12,000 characters), status, created_at, content_expires_at | `arguments` and `result` until `content_expires_at`, within `WORKBENCH_RETENTION_DAYS` (7); the rest with the chat |
| `attachments` | id, chat_id, message_id, workbench_file_id, path, name, size, mime, kind (`document`, `image`), status (`processing`, `ready`, `failed`), render (the manifest: the page count, the pages without text and the sizes of the renders), failure (a token), created_at | with the chat; `render` until the file period (7 days), then emptied ([retention.md](retention.md)) |
| `results` | id, message_id, position, kind (`image`, `table`, `file`), workbench_file_id, path, name, mime, size, preview (a header and at most 5 rows), created_at, preview_expires_at | `preview` until `preview_expires_at`, within `WORKBENCH_RETENTION_DAYS` (7); the rest with the chat |
| `file_marks` | chat_id, created_at: when the chat's workbench folder may have got a file (an attachment, a hand-over, a tool call, a step or a result) | with the chat |
| `retention_periods` | the chat period's state, and the file period the content deadlines were set under | no personal data |

## What is never stored

- **File bytes.** Uploads stream through to workbench and downloads stream back.
- **Image bytes and data URLs.** The renders of a message's pictures and of the
  pages or pictures its tools showed are fetched from workbench and held in
  memory for that one message. A data URL exists only in the request to the
  router: never in the database, a log or the audit.
- **Recordings and transcripts.** A recording is held in memory for its call;
  the transcript goes back to the composer only, and becomes a message only if
  the user sends it.
- **The thinking.** It is never shown, stored, replayed or recorded. Templates
  that always think, or a round cut at `max_tokens` inside a think block, need
  vllm-service's `CHAT_REASONING_PARSER`; otherwise the held reasoning is
  released as text, as in docint.
- **The purpose.** Only in the audit events and in the header to workbench,
  which records it too. The browser keeps it for the tab's session.

Logs hold ids, counts, statuses and tokens only.

## Where data goes

| To | What | Under |
|---|---|---|
| The router, chat (`OPENAI_API_BASE`) | the system prompt (the date and, with tools, the chat's folder and the file period; when workbench offers `write_presentation`, also a fixed rule on presentations, which holds no data), the message with its attachments by name, path and size, the turn's tool calls and results, and earlier turns within the budget; while the model sees, also the attached pictures and the pages or pictures a tool showed, as `image_url` data URLs of workbench's renders (workbench removes their metadata), held for one message, and the earlier turns' pictures while the budget allows | `X-Cache-Scope` (an HMAC of the account under a key that never leaves the process, new at each start), `X-Request-ID` |
| The router, speech-to-text | the recording | the same |
| workbench `/mcp` | tool arguments; results back, with the pages or pictures a tool shows | `X-User-Id` and `X-Session-Id`: HMAC-SHA256 of the account and of the chat id under `WORKBENCH_API_KEY`; the purpose (or `ghostwriter chat`), `X-Request-ID`; `X-Accept-Images: 1` on the discovery always, and on a turn's calls only while the model sees |
| workbench `/files/upload`, `/files/view` | an attachment's bytes, a download's bytes | the same |
| workbench `POST /files/prepare` | the path of a picture or a PDF in the chat's folder, and with a fixed image cost `strips=2` for a large picture; the manifest back | the same |
| workbench `GET /files/view?rendition=` | the number of a page or picture; its render back, for a message's model call or for the owner's preview | the same |
| Ollama `POST /api/show`, on the router's host | the chat model's name only, once at start; only with `INFERENCE_PROVIDER=ollama` and pictures on | the router's key |
| workbench `DELETE /files/chat` | the chat's folder, on the owner's delete, the sweep, or an upload into a chat deleted meanwhile | the same; the sweep as the system |
| obs-plane | the audit chain, references only ([audit.md](audit.md)), kept by obs-plane | the account as actor |

No account name and no chat id reach the router, workbench or Ollama.

workbench 0.7.0 lists `view_file` and returns images only to a call that carries
`X-Accept-Images: 1`; without it, it answers as 0.6.0 did, with no `view_file`,
no images and the old `read_document` text. Ghostwriter 0.3.0 sends the header
on every discovery, so the one cached listing is the full one, and on a turn's
calls only while the model sees (pictures on, a model that takes them, `view_file`
listed). A turn the model refused pictures in goes on without the header.

## A picture or a PDF, after its upload

While pictures are offered, a picture or a PDF is stored `processing` and its
upload answers at once. The attachment processor (`ghostwriter/processing.py`,
the one process, at most two prepares at a time) has workbench prepare it
(`POST /files/prepare`, idempotent) and keeps its manifest as `render`: the
attachment is then `ready`. A picture whose prepare fails is `failed`, with a
token, and is named to the model as unreadable; a PDF is `ready` without a manifest
and reads as text. A message that names a `processing` attachment is answered
409. One found `processing` at start, whose job ended with the last process, is
prepared again.

- **The wait.** The SPA follows the status with a long poll: `GET
  /api/chats/{id}/attachments/{attachment_id}?wait=` answers when the status
  settles, or after at most 20 s, and keeps nothing.
- **The preview.** `GET …/preview?page=` streams one render from workbench's
  view route, with its hardening headers, for a tile or the lightbox. Nothing is
  kept or cached here, and each call is a `disclose` event.
- **Strips.** With `GHOSTWRITER_IMAGE_CONTEXT_TOKENS` set, every image costs the
  same, so one whose long side is over 1024 px goes as two strips to keep its
  detail. After the manifest, the processor asks `prepare(..., strips=2)` once,
  so the first turn finds them cut; if that fails (60 s a read, never retried),
  the picture stays `ready` and workbench cuts the strips as the turn views it.
  Pages go as strips at view time.
- **Without workbench.** With pictures on and workbench off or unreachable, no
  tool is offered and earlier pictures go as "could not be loaded" notes.

## Who can read it

The owner only. Every request first resolves the chat by the request's own
account; another owner's chat answers 404, and there is no admin path or
`owner` parameter. Anyone with root on the host can read the volume.

## Residual

A tool call cut short by the message's wall time may still leave outputs in the
chat's folder without a card, and so may outputs past a message's 20 cards and
those of a regenerated answer. `list_files` shows them; they go with the chat,
or after 7 days.
