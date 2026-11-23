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
| `POST /api/chats/{id}/attachments` | `collect` `chat.attachment` | `attachment` [id] | `disclose` `chat.attachment` (`workbench:upload`), before the upload; an `error` one if it fails; `delete` `chat.workbench` (reason `chat_gone`) for a chat deleted during the upload; for a picture, a PDF or an Office file, `disclose` `chat.attachment` (`workbench:prepare`, criteria `kind`), before the prepare is sent (for an Office file, when its background prepare starts), and an `error` one (`reason`) if it fails; for a video or audio clip, `disclose` `chat.attachment` (`nextext:transcribe`, criteria `kind`, `bytes`) before the clip goes to Nextext, and an `error` one (`reason`) if its round trip there fails, then `disclose` `chat.attachment` (`workbench:prepare`, criteria `kind`) before its transcript and frames go to workbench, and an `error` one (`reason`) if that or the prepare fails; and `delete` `chat.nextext` (target `job` [Nextext's id], criteria `deleted`) once its job at Nextext is deleted, `error` when Nextext could not delete it |
| `DELETE /api/chats/{id}/attachments/{attachment_id}` | `delete` `chat.attachment`, an attachment not yet sent, before anything goes | `attachment` [id], criteria `status` | `delete` `chat.workbench` with the file [workbench id] after workbench's delete: `files_deleted` (0 when workbench held it no more), or an `error` one (`reason`) when workbench could not delete it, the attachment kept |
| `GET /api/chats/{id}/attachments/{attachment_id}` | `query` `chat.attachment`, after its wait | `attachment` [id], criteria `status` | — |
| `GET /api/chats/{id}/attachments/{attachment_id}/preview` | `disclose` `file.preview` (`preview`), criteria `page` | `file` [id] | `error` child on failure |
| `GET /api/chats/{id}/files/{file_id}` | `disclose` `file.download` (`download`) | `file` [id] | `error` child on failure |
| `POST /api/transcribe` | `ai_call` `speech.transcribe` (`inference:asr`) | — | — |
| start-up, a chat delete that failed, or a take-back that failed: a `processing` attachment | `disclose` `chat.attachment` (`workbench:prepare`; for a clip `nextext:transcribe`) as the system, criteria `kind`, `requeued` | `attachment` [id] | an `error` one (`reason`) if the prepare fails; for a clip, the same children as an attached clip's |
| start-up, 4 minutes after a create whose answer was lost, and the sweep: Ghostwriter's jobs at Nextext that no clip owns | `delete` `chat.nextext` as the system, written before the deletes, criteria `listed`, `at_start` | `job` (count) | an `error` one (`job`, count) for the jobs Nextext did not delete |
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
- A video or audio clip goes to Nextext after its upload has answered, on its own
  two slots, so its `nextext:transcribe` child follows the upload's events. Its
  `workbench:prepare` child is written only once Nextext's job completed, also
  one skipped for no speech; the prepare after it is the same hand-over and is
  not recorded again. Its job is deleted at Nextext as soon as its artifacts are
  in workbench, or it failed, or the budget (`NEXTEXT_TIMEOUT`) was spent; the
  `chat.nextext` child records that delete by Nextext's job id only, `deleted:
  true` also for a job Nextext no longer held (it restarted). A clip whose chat
  is deleted, or that is taken back, while it is at Nextext still deletes its job
  and records that `chat.nextext` child, and nothing more. A hand-over to Nextext
  that cannot be written sends nothing (the clip fails with `audit`, without a
  child); one to workbench that cannot be written sends no artifact, and the job
  is still deleted. The `error` child's `reason` is `nextext_unavailable`,
  `nextext_failed`, `undecodable_media`, `timeout`, or workbench's token.
- A reconcile lists Ghostwriter's jobs at Nextext (`listed`: how many) and
  records how many no clip owns before it deletes any, by count only. At start
  (`at_start: true`) it deletes every job listed: each clip waits for it before
  it makes a job, so none is this process's. Four minutes after a create whose
  answer was lost (it timed out or did not read, or the clip was cancelled
  meanwhile, so Nextext may have made a job whose id never came back) it spares
  the jobs younger than 3 minutes, and the sweep's spares those younger than
  `NEXTEXT_TIMEOUT` and 60 s; both spare every job a running clip owns. One
  reconcile runs at a time, so no two count the same job. With nothing to
  delete, or a Nextext that cannot list, it records nothing; a reconcile whose
  record cannot be written deletes nothing (at start, the clips then go on, and
  the sweep goes on to the chats). Its `error` child counts the jobs Nextext did
  not delete, also those a reconcile past its 60 s left; the next reconcile lists
  them again.
- A download cut off after its 200 keeps its `ok` event.
- A sweep event names at most 200 ids. Its `cutoff` is the sweep's own time;
  `period_days` is the configured period, also during a notice.
