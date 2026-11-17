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

/** A file the user attached. */
export interface AttachmentOut {
  id: string
  file_id: string
  name: string
  size: number
  mime: string
  created_at: string
  expired: boolean
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

/** A message with what belongs to it. */
export interface MessageOut {
  id: string
  role: Role
  content: string
  created_at: string
  attachments: AttachmentOut[]
  steps: StepOut[]
  results: ResultOut[]
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
  max_attachment_bytes: number
  max_recording_s: number
  max_recording_bytes: number
  /** Null while chat retention is off. */
  chat_retention_days: number | null
  file_retention_days: number
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

// The SSE frames of /messages and /regenerate. `step` carries a StepOut and
// `result` a ResultOut; `final` is always last.

export interface TokenFrame {
  text: string
}

export interface FinalFrame {
  message_id: string
  /** The message sent, or null for a regenerated answer. */
  user_message_id: string | null
  chat: ChatOut
  stopped: boolean
}

export interface ErrorFrame {
  code: 'model' | 'audit' | 'internal'
  message: string
}
