import { useState, type DragEvent, type ReactNode } from 'react'
import { EmptyState, Spinner } from '@infra/ui'
import { ApiError } from '../api/client'
import type { AppConfig, ErrorFrame } from '../api/types'
import type { ChatStream, StreamFailure } from '../hooks/useChatStream'
import { useChat } from '../hooks/useChats'
import { useStickToBottom } from '../hooks/useStickToBottom'
import type { Strings } from '../i18n'
import { useT } from '../i18n/LanguageContext'
import { DraftView, MessageView, PendingUser } from './MessageView'
import { RequestError } from './RequestError'

const ERROR_KEYS: Record<ErrorFrame['code'], keyof Strings> = {
  model: 'error.model',
  audit: 'error.audit',
  internal: 'error.internal',
}

/** The refusals a send or a regenerate names by their code. */
const REFUSAL_KEYS: Partial<Record<string, keyof Strings>> = {
  busy: 'chat.answer_busy',
  no_answer: 'chat.no_answer',
}

/** Why the last answer ended early, or never started: a refusal says what it
 *  is by its code (`busy`, an answer still being written, which a chat left
 *  and reopened can meet; `no_answer`), anything else is the generic line. */
function FailureLine({ failure }: { failure: StreamFailure }) {
  const t = useT()
  let key: keyof Strings | undefined
  if (failure.kind === 'answer') key = ERROR_KEYS[failure.code]
  else if (failure.error instanceof ApiError && failure.error.code !== null) key = REFUSAL_KEYS[failure.error.code]
  if (key === undefined) {
    return <RequestError error={failure.kind === 'request' ? failure.error : null} className="text-sm text-danger" />
  }
  return (
    <p role="alert" className="text-sm text-danger">
      {t(key)}
    </p>
  )
}

/** Whether a drag carries files, not text or a link dragged out of the page. */
function carriesFiles(event: DragEvent): boolean {
  return Array.from(event.dataTransfer?.types ?? []).includes('Files')
}

/**
 * A chat's messages, the answer being written below them, and why the last
 * one ended early; the view follows new text while the user is at its bottom.
 *
 * A chat with no messages invites a question, and a document only while
 * attachments are on. A regenerate's draft stands where the answer it
 * replaces was. `stream` is the chat's one stream, which `composer`, rendered
 * below the messages, sends on too; `reasoning` is the composer's switch,
 * sent with a regenerate. Files dropped anywhere on it go to `onDropFiles`;
 * without it, a drop is left to the browser.
 */
export function Conversation({
  chatId,
  config,
  stream,
  reasoning,
  composer,
  onDropFiles,
}: {
  chatId: string
  config: AppConfig
  stream: ChatStream
  reasoning: boolean
  composer?: ReactNode
  onDropFiles?: (files: File[]) => void
}) {
  const t = useT()
  const chat = useChat(chatId)
  const [dragging, setDragging] = useState(false)
  const { ref, onScroll } = useStickToBottom<HTMLDivElement>(stream.draft ?? stream.pendingUser ?? chat.data)
  const fileDays = config.file_retention_days

  let messages = chat.data?.messages ?? []
  // The server dropped the answer a regenerate replaces; the draft takes its place.
  if (stream.streaming && stream.pendingUser === null && messages.at(-1)?.role === 'assistant') {
    messages = messages.slice(0, -1)
  }
  const last = messages.at(-1)
  const lastAnswer = !stream.streaming && last?.role === 'assistant' ? last.id : null
  const failure: StreamFailure | null = stream.draft?.error
    ? { kind: 'answer', code: stream.draft.error }
    : stream.failure
  const empty = chat.isSuccess && messages.length === 0 && !stream.streaming

  const drops = onDropFiles && {
    onDragOver: (event: DragEvent<HTMLDivElement>) => {
      if (!carriesFiles(event)) return
      event.preventDefault()
      event.dataTransfer.dropEffect = 'copy'
      setDragging(true)
    },
    onDragLeave: (event: DragEvent<HTMLDivElement>) => {
      if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false)
    },
    onDrop: (event: DragEvent<HTMLDivElement>) => {
      setDragging(false)
      if (!carriesFiles(event)) return
      event.preventDefault()
      onDropFiles(Array.from(event.dataTransfer.files))
    },
  }

  return (
    <div className="relative flex h-full min-h-0 flex-col" {...drops}>
      {dragging && (
        <div className="pointer-events-none absolute inset-2 z-10 flex items-center justify-center rounded-lg border-2 border-dashed border-primary bg-background/90 text-sm text-foreground">
          {t('composer.drop')}
        </div>
      )}
      <div ref={ref} onScroll={onScroll} className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 px-4 py-6">
          {chat.isPending && <Spinner label={t('chat.loading_messages')} />}
          {chat.isError && <RequestError error={chat.error} className="text-sm text-danger" />}
          {empty && <EmptyState>{t(config.attachments ? 'chat.empty' : 'chat.empty_text_only')}</EmptyState>}
          {messages.map((message) => (
            <MessageView
              key={message.id}
              chatId={chatId}
              message={message}
              fileDays={fileDays}
              onRegenerate={message.id === lastAnswer ? () => void stream.regenerate(reasoning) : undefined}
            />
          ))}
          {stream.pendingUser !== null && <PendingUser message={stream.pendingUser} />}
          {stream.draft && <DraftView chatId={chatId} draft={stream.draft} fileDays={fileDays} />}
          {failure && <FailureLine failure={failure} />}
        </div>
      </div>
      {composer}
    </div>
  )
}
