import type { ReactNode } from 'react'
import { EmptyState, Spinner } from '@infra/ui'
import { ApiError } from '../api/client'
import type { AppConfig, ErrorFrame } from '../api/types'
import { useChatStream, type ChatStream, type StreamFailure } from '../hooks/useChatStream'
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

/** Why the last answer ended early, or never started: a 409 is the chat's
 *  answer still being written, which a chat left and reopened can meet. */
function FailureLine({ failure }: { failure: StreamFailure }) {
  const t = useT()
  const busy = failure.kind === 'request' && failure.error instanceof ApiError && failure.error.status === 409
  if (failure.kind === 'request' && !busy) return <RequestError error={failure.error} className="text-sm text-danger" />
  return (
    <p role="alert" className="text-sm text-danger">
      {t(failure.kind === 'answer' ? ERROR_KEYS[failure.code] : 'chat.answer_busy')}
    </p>
  )
}

/**
 * A chat's messages, the answer being written below them, and why the last
 * one ended early; the view follows new text while the user is at its bottom.
 *
 * A chat with no messages invites a question, and a document only while
 * attachments are on. A regenerate's draft stands where the answer it
 * replaces was. `reasoning` is the composer's switch, sent with a regenerate;
 * `composer` renders below the messages over the same stream.
 */
export function Conversation({
  chatId,
  config,
  reasoning,
  composer,
}: {
  chatId: string
  config: AppConfig
  reasoning: boolean
  composer?: (stream: ChatStream) => ReactNode
}) {
  const t = useT()
  const chat = useChat(chatId)
  const stream = useChatStream(chatId)
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

  return (
    <div className="flex h-full min-h-0 flex-col">
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
      {composer?.(stream)}
    </div>
  )
}
