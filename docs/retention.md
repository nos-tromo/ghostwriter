# Retention

| What | Kept | How it goes |
|---|---|---|
| A chat: its messages, steps, attachments and results | `GHOSTWRITER_CHAT_RETENTION_DAYS` (90) after it started | the daily sweep deletes it |
| Tool contents (a step's arguments and result) and table previews | `WORKBENCH_RETENTION_DAYS` (7), counted from the chat's earliest file | read as expired at once; the daily sweep empties them |
| The files | 7 days, in workbench | workbench's own retention; here their cards read "expired" and a download answers 410 |

Using or continuing a chat never extends its period. The sweep runs a minute
after start, then daily (`ghostwriter/retention.py`).

## The chat period and its notice

Each start puts the configured period in force by docint's rule
(`ghostwriter/retention_rule.py`, as open-webui-service's `StoreRule`):

| Change | Effect |
|---|---|
| First start, or on again after `off` | nothing is deleted for `RETENTION_NOTICE_DAYS` (30) |
| A longer period | applies at once |
| A shorter period | the notice holds only what it makes due sooner; the period before keeps deleting meanwhile |
| The notice alone | moves nothing |

Example: 90 days shortened to 60 on 1 March. A chat started 70 days before goes
on 21 March, its old date; one started 50 days before on 31 March, the end of
the notice, not on 11 March; one started 20 days before on 10 April, its new date.

## Tool contents and previews

They expire `WORKBENCH_RETENTION_DAYS` after the chat's earliest file (its
first attachment, tool step or result), or after they were made if that is
earlier, so nothing derived from a file outlives it. Example: first file on
day 0, a step on day 6: both go on day 7. From then on the trail reads "Details
deleted after 7 days", follow-ups replay nothing of them, and the model is told
that earlier files have expired. The tool, status, file name, size and type stay.
A changed file period applies at the next start to what has not expired yet,
without a notice; nothing expired comes back.

## The delete order

1. Hold the chat's turn: while an answer runs, the owner's delete gets 409 and
   the sweep leaves the chat to its next run.
2. Record the `delete` event.
3. Delete the chat's folder in workbench (`DELETE /files/chat`), then record what went.
4. Delete the chat's rows. The owner's delete stops when workbench fails (502,
   the chat is kept); the sweep goes on, as the chat is due and workbench's own
   period takes its files.

Tool contents are recorded, then emptied. Deleted rows are overwritten
(`secure_delete`), and each sweep ends with a write-ahead-log checkpoint.

A file attached by mistake is taken back by deleting the chat: workbench offers
this caller no single-file delete. Otherwise it goes after its 7 days.

## The dry run

`make retention-dry-run` asks the running backend what a sweep would delete
now, in counts, ages and dates only; `CHAT_DAYS=n` previews another chat
period, with the notice it would get. It records and changes nothing.

```text
chats due now: 2 (started 95 to 120 days ago; period 90 days, in force since 2026-11-16)
tool contents due now: 3 steps, 1 preview (made 8 to 9 days ago; period 7 days)
This run deletes nothing.
```

## The banner

Above the conversation: "Chats are deleted 90 days after they were started.
Files and tool results are deleted after 7 days." It leaves out the chat
period while chat retention is off, and the file period while attachments are
off. Under attached files the composer adds "Attached files stay in this chat
until it is deleted, for 7 days at most."
