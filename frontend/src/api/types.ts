// Mirrors ghostwriter/schemas.py, ghostwriter/api/meta.py and the request
// bodies and SSE frames of ghostwriter/api/. Field names match the JSON
// exactly; datetimes arrive as ISO 8601 strings.

export type Role = 'user' | 'assistant'
export type StepStatus = 'running' | 'ok' | 'error'
export type ResultKind = 'image' | 'table' | 'file'

/** A chat in the list. `title` is null until its first message or a rename;
 *  `expires_at` is null while retention is off. */
export interface ChatOut {
  id: string
  title: string | null
  created_at: string
  updated_at: string
  expires_at: string | null
}

export type AttachmentKind = 'document' | 'image' | 'media'
export type AttachmentStatus = 'processing' | 'ready' | 'failed'

/** What a document is counted in. */
export type MeasureUnit = 'page' | 'slide' | 'paragraph' | 'row' | 'line'

/** How long a document is, as workbench measured it: `pending` while it is measured, `measured`, or `none`
 *  when it could not be. `total` and `unit` are null until measured; `too_long` is whether the document is
 *  longer than one prompt holds, so the model reads it only in parts. */
export interface AttachmentLength {
  state: 'pending' | 'measured' | 'none'
  total: number | null
  unit: MeasureUnit | null
  too_long: boolean
}

/** A file the user attached. `kind` says what it is (a picture is shown, and
 *  seen by a model that can); `status` how far its preparation has come: only
 *  a `ready` file can be sent with a message. `length` is a document's measure,
 *  null for a file never measured (a picture, a clip, a file stored before
 *  measuring began). */
export interface AttachmentOut {
  id: string
  file_id: string
  name: string
  size: number
  mime: string
  created_at: string
  expired: boolean
  kind: AttachmentKind
  status: AttachmentStatus
  length: AttachmentLength | null
}

/** A tool step of an answer; `detail`, `arguments` and `result` are null once expired. */
export interface StepOut {
  id: string
  tool: string
  status: StepStatus
  detail: string | null
  arguments: string | null
  result: string | null
  expired: boolean
}

/** The head of a table a tool wrote (`TablePreviewOut`). `row_count` counts
 *  the data rows below the header, when known. */
export interface TablePreview {
  columns: string[]
  rows: string[][]
  row_count: number | null
  truncated: boolean
}

/** A file a tool wrote, as a card below the answer. */
export interface ResultOut {
  id: string
  kind: ResultKind
  file_id: string
  name: string
  mime: string
  size: number
  preview: TablePreview | null
  expired: boolean
}

/** A message with what belongs to it. `notices` is what the answer says about
 *  itself, stored with it; null for an answer written before notices were kept
 *  (and for a question). `context_estimate` is the estimated size of the next
 *  prompt in tokens, null where none was made. */
export interface MessageOut {
  id: string
  role: Role
  content: string
  created_at: string
  attachments: AttachmentOut[]
  steps: StepOut[]
  results: ResultOut[]
  notices: NoticeOut[] | null
  context_estimate: number | null
}

/** A chat as its owner opens it. */
export interface ChatDetail {
  chat: ChatOut
  messages: MessageOut[]
  pending_attachments: AttachmentOut[]
}

/** What the SPA needs to know of this deployment (`ConfigOut`). */
export interface AppConfig {
  ui_language: 'en' | 'de'
  /** The audit actions of the routes that need a stated purpose, sorted. */
  require_purpose: string[]
  speech_input: boolean
  /** Whether files can be attached, that is whether workbench is configured. */
  attachments: boolean
  attachment_types: string[]
  /** The picture types among `attachment_types`; empty while none can be attached. */
  image_types: string[]
  /** The video and audio types among `attachment_types`; empty while clips are not offered. */
  media_types: string[]
  /** Whether video and audio can be attached: Nextext is reachable and workbench is new enough. */
  media: boolean
  /** The most pictures one message may carry. */
  max_images: number
  /** Whether the model can see pictures. */
  vision: boolean
  max_attachment_bytes: number
  max_recording_s: number
  max_recording_bytes: number
  /** Null while chat retention is off. */
  chat_retention_days: number | null
  file_retention_days: number
  /** The most tokens one round's prompt may hold, for a message without reasoning: the model's context less
   *  the answer and a margin. A chat's context estimate is read against it. */
  prompt_tokens: number
  /** The same for a message with reasoning on, whose answer may be longer. */
  reasoning_prompt_tokens: number
  version: string
}

/** The account a request acts as (`WhoamiOut`); both null without a trusted identity. */
export interface Whoami {
  username: string | null
  display_name: string | null
}

/** A message to send (`MessageIn`). */
export interface MessageIn {
  content: string
  attachment_ids: string[]
  reasoning: boolean
}

/** How to answer the last question again (`RegenerateIn`). */
export interface RegenerateIn {
  reasoning: boolean
}

/** A recording's transcript (`TranscriptOut`). */
export interface TranscriptOut {
  text: string
}

// The SSE frames of /messages and /regenerate. `start` is always first,
// `step` carries a StepOut and `result` a ResultOut; `final` is always last.

/** The rows the turn stored before it streams: the answer, and the message sent (null for a regenerate). */
export interface StartFrame {
  message_id: string
  user_message_id: string | null
}

export interface TokenFrame {
  text: string
}

export interface FinalFrame {
  message_id: string
  /** The message sent, or null for a regenerated or continued answer. */
  user_message_id: string | null
  chat: ChatOut
  stopped: boolean
  /** The estimated tokens of the next prompt; null where none was made. */
  context_estimate: number | null
}

/** Why the answer ended early; `context` is the router refusing a round as
 *  longer than the model's context, with no picture left to drop. */
export interface ErrorFrame {
  code: 'model' | 'audit' | 'internal' | 'context'
  message: string
}

/** What the answer says about itself: `answer_cut` (it ended at the length
 *  limit), `document_partial` (a document was read in part), `document_unread`
 *  (an attachment was not opened), `images_dropped` (the model took no pictures
 *  and answered without them), `context_trimmed` (earlier turns were left out
 *  of the prompt) and `tools_unavailable` (workbench could not be reached, so
 *  the turn ran with no tool; its code alone). A code this app does not know is
 *  ignored. */
export type NoticeCode =
  | 'answer_cut'
  | 'document_partial'
  | 'document_unread'
  | 'images_dropped'
  | 'context_trimmed'
  | 'tools_unavailable'

/** One notice, as stored with an answer and as its live frame carries it. The
 *  details of a document notice are null once they have expired, and a notice
 *  of another kind carries none. */
export interface NoticeOut {
  code: NoticeCode
  file_id?: string | null
  name?: string | null
  read?: number | null
  total?: number | null
  unit?: MeasureUnit | null
}

export type NoticeFrame = NoticeOut
