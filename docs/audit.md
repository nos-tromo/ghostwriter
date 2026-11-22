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
- **Purpose.** From the SPA's purpose bar (`X-Audit-Purpose`), which the SPA
  shows only while `AUDIT_REQUIRE_PURPOSE` lists actions; without that, no
  purpose is stated. A request whose actions `AUDIT_REQUIRE_PURPOSE` lists and
  that states none gets 428 and a `denied` event. workbench gets the stated purpose, or the reason
  `ghostwriter chat`.
- **Request id.** The gateway's `X-Request-ID`, minted when none (or an invalid
  one) arrives; it goes on to the router and workbench. Each sweep batch has its own.

## Catalogue

`ai_call` events name the output by its hash only; `chat.answer` carries
`reasoning: true|false` and `images` (how many images the round sent) in its
criteria, never the thinking or an image. A round the model refused with its
images, sent again without them, is two `chat.answer` events.

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
| `POST /api/chats/{id}/attachments` | `collect` `chat.attachment` | `attachment` [id] | `disclose` `chat.attachment` (`workbench:upload`), before the upload; an `error` one if it fails; `delete` `chat.workbench` (reason `chat_gone`) for a chat deleted during the upload; for a picture, a PDF or an Office file, `disclose` `chat.attachment` (`workbench:prepare`, criteria `kind`), before the prepare is sent (for an Office file, when its background prepare starts), and an `error` one (`reason`) if it fails |
| `DELETE /api/chats/{id}/attachments/{attachment_id}` | `delete` `chat.attachment`, an attachment not yet sent, before anything goes | `attachment` [id], criteria `status` | `delete` `chat.workbench` with the file [workbench id] after workbench's delete: `files_deleted` (0 when workbench held it no more), or an `error` one (`reason`) when workbench could not delete it, the attachment kept |
| `GET /api/chats/{id}/attachments/{attachment_id}` | `query` `chat.attachment`, after its wait | `attachment` [id], criteria `status` | — |
| `GET /api/chats/{id}/attachments/{attachment_id}/preview` | `disclose` `file.preview` (`preview`), criteria `page` | `file` [id] | `error` child on failure |
| `GET /api/chats/{id}/files/{file_id}` | `disclose` `file.download` (`download`) | `file` [id] | `error` child on failure |
| `POST /api/transcribe` | `ai_call` `speech.transcribe` (`inference:asr`) | — | — |
| start-up, a chat delete that failed, or a take-back that failed: a `processing` attachment | `disclose` `chat.attachment` (`workbench:prepare`) as the system, criteria `kind`, `requeued` | `attachment` [id] | an `error` one (`reason`) if the prepare fails |
| the sweep | `delete` `retention.chat` | `chat` | `delete` `retention.chat.workbench` |
| the sweep | `delete` `retention.tool_content` | `tool_step` / `result` / `attachment` (manifests) | — |

`/api/config`, `/api/whoami`, `/api/health` and `/metrics` are not audited. A
refusal before the action (400, 401, 404, 409, 410, 413, 415, 422, 503) records nothing.

## Reading it

- A primary event records an authorised attempt. A chat its owner deletes is
  gone only once its `chat.workbench` child is `ok` and its rows went; the sweep
  deletes the rows also when workbench failed (`retention.chat.workbench`
  `error`), and workbench's own period takes the files. A chat that never used
  workbench goes without it (`workbench: not_used`). A retry after a 502, or
  the next sweep after an interrupted one, writes a second primary.
- A failed upload records `disclose` `ok`, then `error`. A refusal after the
  hand-over leaves `collect` / `disclose` `ok` for an attachment without a row;
  the file stays in its chat's folder until the chat goes, or for 7 days.
- A picture, a PDF or an Office file is prepared after its upload has answered,
  so its `workbench:prepare` child follows the upload's events, possibly after
  other requests' events. An Office file is prepared in the background, one at a
  time, so its child is recorded when that prepare starts, which may be well
  after the upload answered; if the prepare fails, an `error` child follows and
  the attachment stays `ready` without a manifest, never asked again. A prepare
  whose hand-over cannot be written is not sent (the attachment settles with the
  token `audit`, without a child; an Office file stays `ready`). A prepare tried
  again after a busy workbench is the same hand-over, recorded once. A chat
  deleted meanwhile cancels the job: no `error` child follows. If that delete
  then fails (a 502, or a record that cannot be written) or a sweep batch does,
  the chat stays and its `processing` attachments are handed over again, as at
  start-up (`requeued`).
- A download cut off after its 200 keeps its `ok` event.
- A sweep event names at most 200 ids. Its `cutoff` is the sweep's own time;
  `period_days` is the configured period, also during a notice.
