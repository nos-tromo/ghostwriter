# Data flow

The record of processing: what Ghostwriter keeps, for how long, where data
goes, and who can read it.

## What is stored

SQLite on the `ghostwriter-data` volume (`ghostwriter/models.py`).

| Table | Columns | Kept |
|---|---|---|
| `chats` | id, owner (the account), title (the first message's first line, or the owner's), created_at, updated_at, expires_at, first_file_at | until `expires_at` (90 days after the start, [retention.md](retention.md)) or the owner's delete |
| `messages` | id, chat_id, position, role (`user`, `assistant`), content (the text sent, and the answer shown), created_at | with the chat |
| `tool_steps` | id, message_id, position, round, call_id, tool, arguments (the model's, code included), result (what the model read back, at most 12,000 characters), status, created_at, content_expires_at | `arguments` and `result` until `content_expires_at`, within `WORKBENCH_RETENTION_DAYS` (7); the rest with the chat |
| `attachments` | id, chat_id, message_id, workbench_file_id, path, name, size, mime, created_at | with the chat |
| `results` | id, message_id, kind (`image`, `table`, `file`), workbench_file_id, path, name, mime, size, preview (a header and at most 5 rows), created_at, preview_expires_at | `preview` until `preview_expires_at`, within `WORKBENCH_RETENTION_DAYS` (7); the rest with the chat |
| `retention_periods` | the chat period's state | no personal data |

## What is never stored

- **File bytes.** Uploads stream through to workbench and downloads stream back.
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
| The router, chat (`OPENAI_API_BASE`) | the system prompt (the date and, with tools, the chat's folder and the file period), the message with its attachments by name, path and size, the turn's tool calls and results, and earlier turns within the budget | `X-Cache-Scope` (an HMAC of the account under a key that never leaves the process, new at each start), `X-Request-ID` |
| The router, speech-to-text | the recording | the same |
| workbench `/mcp` | tool arguments; results back | `X-User-Id` and `X-Session-Id`: HMAC-SHA256 of the account and of the chat id under `WORKBENCH_API_KEY`; the purpose (or `ghostwriter chat`), `X-Request-ID` |
| workbench `/files/upload`, `/files/view` | an attachment's bytes, a download's bytes | the same |
| workbench `DELETE /files/chat` | the chat's folder, on the owner's delete, the sweep, or an upload into a chat deleted meanwhile | the same; the sweep as the system |
| obs-plane | the audit chain, references only ([audit.md](audit.md)), kept by obs-plane | the account as actor |

No account name and no chat id reach the router or workbench.

## Who can read it

The owner only. Every lookup names the request's own account; another owner's
chat answers 404, and there is no admin path or `owner` parameter. Anyone with
root on the host can read the volume.

## Residual

A tool call cut short by the message's wall time may still leave outputs in the
chat's folder without a card, and so may outputs past a message's 20 cards.
`list_files` shows them; they go with the chat, or after 7 days.
