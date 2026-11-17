import { Badge, CopyButton, RefreshButton, Spinner, UserBubble } from '@infra/ui'
import type { AttachmentOut, MessageOut } from '../api/types'
import type { DraftAnswer, PendingMessage } from '../hooks/useChatStream'
import { useLang, useT } from '../i18n/LanguageContext'
import { formatSize } from '../lib/format'
import { MarkdownProse } from './MarkdownProse'
import { ResultCard } from './ResultCard'
import { Trail } from './Trail'

/** A file the message bound, as a chip that reads on the blue of the user's bubble. */
function AttachmentChip({ attachment }: { attachment: AttachmentOut }) {
  const t = useT()
  const lang = useLang()
  return (
    <span className="inline-flex max-w-full items-center gap-1 rounded-full border border-bubble-foreground/30 bg-bubble-foreground/15 px-2 py-0.5 text-xs">
      <span title={attachment.name} className="min-w-0 truncate">
        {attachment.name}
      </span>
      <span className="shrink-0 text-bubble-foreground/80">{formatSize(attachment.size, lang)}</span>
      {attachment.expired && <Badge>{t('result.expired')}</Badge>}
    </span>
  )
}

/** What the user wrote, as written (never Markdown), and the files it bound. */
function UserMessage({ message }: { message: Pick<MessageOut, 'content' | 'attachments'> }) {
  return (
    <UserBubble data-role="user">
      {message.content && <p className="whitespace-pre-wrap break-words">{message.content}</p>}
      {message.attachments.length > 0 && (
        <ul className="flex flex-wrap gap-1">
          {message.attachments.map((attachment) => (
            <li key={attachment.id} className="max-w-full">
              <AttachmentChip attachment={attachment} />
            </li>
          ))}
        </ul>
      )}
    </UserBubble>
  )
}

/** The pending answer before its first token: thinking, for a reasoning answer. */
function Pending({ reasoning }: { reasoning: boolean }) {
  const t = useT()
  const label = reasoning ? t('chat.thinking') : t('chat.answer_pending')
  return (
    <div className="flex items-center gap-2 text-sm text-muted-foreground">
      <Spinner label={label} />
      {/* The spinner announces the same words. */}
      <span aria-hidden="true">{label}</span>
    </div>
  )
}

/**
 * One message: the user's text with its attachments, or an answer with its
 * trail, its text as Markdown and its result cards. An answer with text can
 * be copied as its Markdown; the last answer offers to be regenerated
 * (`onRegenerate`).
 */
export function MessageView({
  chatId,
  message,
  fileDays,
  onRegenerate,
}: {
  chatId: string
  message: Pick<MessageOut, 'role' | 'content' | 'attachments' | 'steps' | 'results'>
  fileDays: number
  onRegenerate?: () => void
}) {
  const t = useT()
  if (message.role === 'user') return <UserMessage message={message} />
  return (
    <div data-role="assistant" className="space-y-2">
      <Trail steps={message.steps} fileDays={fileDays} />
      {message.content && <MarkdownProse text={message.content} />}
      {message.results.map((result) => (
        <ResultCard key={result.id} chatId={chatId} result={result} />
      ))}
      {(message.content || onRegenerate) && (
        <div className="flex justify-end gap-1">
          {message.content && (
            <CopyButton text={message.content} label={t('chat.copy_answer')} copiedLabel={t('common.copied')} />
          )}
          {onRegenerate && <RefreshButton label={t('chat.regenerate')} onClick={onRegenerate} />}
        </div>
      )}
    </div>
  )
}

/** The answer being written: its steps as they run, its text so far, its cards. */
export function DraftView({ chatId, draft, fileDays }: { chatId: string; draft: DraftAnswer; fileDays: number }) {
  return (
    <div data-role="assistant" aria-busy="true" className="space-y-2">
      <Trail steps={draft.steps} fileDays={fileDays} live />
      {draft.text ? <MarkdownProse text={draft.text} /> : !draft.error && <Pending reasoning={draft.reasoning} />}
      {draft.results.map((result) => (
        <ResultCard key={result.id} chatId={chatId} result={result} />
      ))}
    </div>
  )
}

/** The message being sent, with the files it binds, shown until the chat is read again. */
export function PendingUser({ message }: { message: PendingMessage }) {
  return <UserMessage message={message} />
}
