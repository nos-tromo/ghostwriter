# Audit log

The emitter is `ghostwriter` / `backend`: one hash-chained chain per process,
written to the container's stdout (`AUDIT_OUTPUT=container`), which obs-plane
collects. Start-up writes `admin` / `config.audit` with the operating mode.

- **References only.** Ids, counts, MIME types and sizes; never a title, a
  message, a file name, code or a transcript. The actor is the account
  (`X-Auth-User`); the sweep and start-up act as the system.
- **Fail closed.** An event that cannot be written stops its action: a request
  gets `500 Request failed.`, an answer ends with the `audit` error frame, a
  sweep batch deletes nothing.
- **Purpose.** Optional, from the SPA's purpose bar (`X-Audit-Purpose`). A
  request whose actions `AUDIT_REQUIRE_PURPOSE` lists and that states none gets
  428 and a `denied` event. workbench gets the stated purpose, or the reason
  `ghostwriter chat`.
- **Request id.** The gateway's `X-Request-ID`, minted when none (or an invalid
  one) arrives; it goes on to the router and workbench. Each sweep batch has its own.

## Catalogue

`ai_call` events name the output by its hash only; `chat.answer` carries
`reasoning: true|false` in its criteria, never the thinking.

| Route | Action / operation | Target | Children |
|---|---|---|---|
| `GET /api/chats` | `query` `chat.list` | `chat` (count) | — |
| `POST /api/chats` | `collect` `chat.create`, written before the chat exists | `chat` [id] | — |
| `GET /api/chats/{id}` | `query` `chat.read` | `chat` [id] | — |
| `PATCH /api/chats/{id}` | `modify` `chat.rename`, written before the rename | `chat` [id] | — |
| `DELETE /api/chats/{id}` | `delete` `chat.delete`, written first | `chat` [id] | `delete` `chat.workbench` |
| `POST /api/chats/{id}/messages` | `collect` `chat.message` | `message` [user id] | `ai_call` `chat.answer` per round; `disclose` `chat.tool` per call attempt, before it is sent |
| `POST /api/chats/{id}/regenerate` | `modify` `chat.regenerate` | `message` [replaced id] | as messages |
| `POST /api/chats/{id}/stop` | none | — | — |
| `POST /api/chats/{id}/attachments` | `collect` `chat.attachment` | `attachment` [id] | `disclose` `chat.attachment` (`workbench:upload`), before the upload; an `error` one if it fails; `delete` `chat.workbench` (reason `chat_gone`) for a chat deleted during the upload |
| `GET /api/chats/{id}/files/{file_id}` | `disclose` `file.download` (`download`) | `file` [id] | `error` child on failure |
| `POST /api/transcribe` | `ai_call` `speech.transcribe` (`inference:asr`) | — | — |
| the sweep | `delete` `retention.chat` | `chat` | `delete` `retention.chat.workbench` |
| the sweep | `delete` `retention.tool_content` | `tool_step` / `result` | — |

`/api/config`, `/api/whoami`, `/api/health` and `/metrics` are not audited. A
refusal before the action (400, 401, 404, 409, 410, 413, 415, 422, 503) records nothing.

## Reading it

- A primary event records an authorised attempt. A chat its owner deletes is
  gone only once its `chat.workbench` child is `ok` and its rows went; the sweep
  deletes the rows also when workbench failed (`retention.chat.workbench`
  `error`), and workbench's own period takes the files. A retry after a 502, or
  the next sweep after an interrupted one, writes a second primary.
- A failed upload records `disclose` `ok`, then `error`. A refusal after the
  hand-over leaves `collect` / `disclose` `ok` for an attachment without a row;
  the file stays in its chat's folder until the chat goes, or for 7 days.
- A download cut off after its 200 keeps its `ok` event.
- A sweep event names at most 200 ids. Its `cutoff` is the sweep's own time;
  `period_days` is the configured period, also during a notice.
