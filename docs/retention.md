# Retention

| What | Kept | How it goes |
|---|---|---|
| A chat: its messages, steps, attachments and results | `GHOSTWRITER_CHAT_RETENTION_DAYS` (90) after it started | the daily sweep deletes it |
| Tool contents (a step's arguments and result) and table previews | `WORKBENCH_RETENTION_DAYS` (7), or less: never past a file they may come from | read as expired at once; the daily sweep empties them |
| An attachment's manifest (its page count, the pages without text, the sizes of its renders; a clip's length, frame count and whether it has a transcript) and a document's measure (its count and unit, the characters of a full read, whether the count was capped) | `WORKBENCH_RETENTION_DAYS` (7) after the attachment was made | the daily sweep empties them; the attachment itself stays with its chat, and its measure's state with it |
| An answer's notices about a file (`document_partial`, `document_unread`: the file's id, how much of it was read, of how much, in which unit) | `WORKBENCH_RETENTION_DAYS` (7) after the answer was made, or less: never past a file it may come from | read as the bare code at once; the daily sweep strips them to their codes. The answer, the codes and its estimate of the next prompt stay with the chat |
| The files, and the renders workbench made of them (a clip's transcript and frames are derived files of the clip) | 7 days, in workbench | workbench's own retention; here their cards read "expired" and a download or a preview answers 410 |
| A clip's copy at Nextext, and what Nextext made of it | one job: until its transcript and frames are in workbench, it failed, or `NEXTEXT_TIMEOUT` (900 s) is spent; a job still running then keeps the upload at Nextext until its pipeline ends | Ghostwriter deletes the job at once; one whose delete failed goes 4 minutes later, one a crash left at the next start, or with the daily sweep |

Using or continuing a chat never extends its period. The sweep runs a minute
after start, then daily (`ghostwriter/retention.py`). A line under the composer
and a line under attached files state the periods, leaving out the chat period
while chat retention is off and the file period while attachments are off.

## The chat period and its notice

Each start puts the configured period in force (docint's rule, `ghostwriter/retention_rule.py`):

| Change | Effect |
|---|---|
| First start, or on again after `off` | nothing is deleted for `RETENTION_NOTICE_DAYS` (30) |
| A longer period | applies at once (or when a running notice ends) |
| A shorter period | the notice holds only what it makes due sooner; the period before keeps deleting meanwhile |

Example: 90 days shortened to 60 on 1 March. A chat started 70 days before goes
on 21 March, its old date; one started 50 days before on 31 March, the end of
the notice, not on 11 March; one started 20 days before on 10 April, its new date.

## Tool contents and previews

They expire `WORKBENCH_RETENTION_DAYS` after they were made, or with the
earliest file that may still be in the chat's folder then, if that goes first,
so nothing derived from a file outlives it. Example: a file on day 0; a step on
day 6 goes on day 7, a step on day 10 on day 17. Emptied, a step reads "Details
deleted after 7 days". A changed file period applies at the next start to what
has not expired yet, without a notice; nothing expired comes back.

## Manifests and renders

An attachment's manifest is derived from its file, so it goes with the file's
period: the sweep records it, then empties it, and the attachment stays. So does
a document's measure, its length (workbench 0.10.0): it is not served once the
file's period has passed (the SPA is served no length), and the sweep empties it
with the manifest. Its state (`measured`, `none`) stays; a measure still `pending`
at start is measured again, or settled `none` while workbench is off. The
renders themselves (the pages and pictures the model sees) are workbench's
derived files and are deleted with their source there. Ghostwriter keeps none:
the processing and preview routes hold nothing (the preview streams a render
through), and a picture's bytes live in memory for the one message that sends it.
After the period an earlier picture is read as expired: the follow-up's line says
so and nothing is fetched.

## An answer's notices

An answer is stored with its notices ([data-flow.md](data-flow.md#what-the-user-is-told)). Of a notice about a
file, its file id and its numbers (how much was read, of how much, in which unit) are content made with the answer,
derived from the file's text, so they expire like a step's arguments and result: `WORKBENCH_RETENTION_DAYS` after the
answer was made, or with the earliest file that may still be in the chat's folder then, if that goes first. The
deadline is set by the first write of the answer that holds such a notice and stays through a Continue of the same
answer. A changed file period moves it at the next start, as it moves a step's, without a notice.

Past it the answer is served the bare code, which says "A document was read only in part" or "An attachment was not
read for this answer", with neither the file's name nor a number, and the sweep strips the stored notice to its code.
The name is never stored: it is read from the chat's attachment row while the details are served. The answer, the
codes (`answer_cut`, `images_dropped`, `context_trimmed` and `tools_unavailable` hold no file or number, and so no
deadline) and the estimate of the next prompt, an integer, stay with the chat until its own period ends. A notice is
a code, an id and counts, never content.

## The delete order

1. Hold the chat's turn: while an answer runs, the owner's delete gets 409 and
   the sweep leaves the chat to its next run.
2. Record the `delete` event.
3. Cancel the jobs that prepare the chat's attachments (a clip's job at Nextext is
   still deleted, [below](#clips-at-nextext)), then delete the chat's
   folder in workbench (`DELETE /files/chat`), and record what went. A chat that
   never used workbench is not sent. A delete that fails after the cancel keeps the
   chat, and its `processing` attachments are prepared again.
4. Delete the chat's rows. The owner's delete stops when workbench fails (502,
   the chat is kept); the sweep goes on, as the chat is due and workbench's own
   period takes its files.

Tool contents are recorded, then emptied. Deleted rows are overwritten
(`secure_delete`), and each sweep ends with a write-ahead-log checkpoint.

A file attached by mistake is taken back before it is sent: the X on its tile
deletes it in workbench with its renders (workbench 0.8.1), and then its row; a
job preparing it is cancelled. A sent file stays with its message until the chat
is deleted or its 7 days are over.

## Clips at Nextext

A video or audio clip goes to Nextext for one job, and Ghostwriter deletes that
job as soon as its transcript and frames are in workbench, it failed, or its
budget was spent, also when the clip is taken back or its chat deleted
meanwhile. Nextext's `DELETE` of a running job drops it from Nextext's list at
once, but its pipeline runs on and keeps Nextext's one queue slot, and Nextext
removes the job's upload when the pipeline ends (in the spike, about 85 s
later). So a clip taken back, whose chat is deleted, or whose budget was spent
while it is transcribed stays at Nextext until its job ends there. Nextext holds its jobs in memory, so
its own restart drops them too.

A job no clip owns is deleted by a reconcile, recorded first (`docs/audit.md`):

- A crash or restart while a clip was at Nextext leaves its job until the next
  start. Once Nextext passes its readiness probe (within 120 s), the start
  reconcile deletes every job Nextext lists for Ghostwriter, before any clip
  makes one; the clip left `processing` then goes to Nextext again, for a new
  job.
- A create whose answer was lost (it timed out or did not read, or the clip was
  cancelled meanwhile) may leave a job whose id never came back. A reconcile 4
  minutes later deletes it, sparing jobs younger than 3 minutes (another clip's
  create may still be answering). Nextext stamps a job once it has stored the
  upload, which can be after Ghostwriter gave up, so the extra minute lets the
  lost job be past the grace by then.
- A clip's own delete that Nextext refuses, or does not answer within 10 s,
  leaves its job, which no clip owns any more: the same reconcile 4 minutes
  later deletes it.
- The daily sweep deletes every job no clip owns that is older than
  `NEXTEXT_TIMEOUT` and 60 s: no clip's job lives that long. It is what catches
  the rest, so these leave a job at Nextext until the next daily sweep after it
  is that old: a probe that does not pass within 120 s of start, and a job the
  reconcile after a lost create or a failed delete did not reach (a Nextext down
  or slow then).

One reconcile runs at a time, and the sweep's takes at most 60 s; whatever
happens there, the sweep then goes on to the chats.

Nextext keeps a service's jobs under the name of its entry in
`NEXTEXT_SERVICE_TOKENS`, not under its token: `ghostwriter=<token>` is the owner
`service:ghostwriter`. A Ghostwriter's start deletes every job of that owner. So
a second Ghostwriter needs an entry of its own name and its own token (say
`ghostwriter-staging=<token>`, that token its `NEXTEXT_API_KEY`): under the same
name, even with another token, it would delete this one's jobs when it starts.

## The dry run

`make retention-dry-run` asks the running backend what a sweep would delete
now, in counts, ages and dates only: the chats, and the tool steps, previews,
manifests (an attachment's manifest or measure, counted once) and answers'
notices (those whose file details are due) whose period has passed; `CHAT_DAYS=n` previews
another chat period, with the notice it would get. It records and changes nothing.
The file it reads may be of an older schema: a column it lacks counts nothing.
