import { Badge, CopyButton, RefreshButton, Spinner, UserBubble } from '@infra/ui'
import type { AttachmentOut, MessageOut } from '../api/types'
import type { DraftAnswer, PendingMessage } from '../hooks/useChatStream'
import { useLang, useT } from '../i18n/LanguageContext'
import { formatSize } from '../lib/format'
import { AttachmentImage } from './AttachmentImage'
import { MarkdownProse } from './MarkdownProse'
import { NoticeLines } from './NoticeLines'
import { ResultCard } from './ResultCard'
import { Trail } from './Trail'

/** A file the message bound, as a chip that reads on the accent of the user's bubble. */
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

/** What the user wrote, as written (never Markdown), and the files it bound: pictures as a
 *  grid of tiles that open full size, every other file as a chip. */
function UserMessage({ chatId, message }: { chatId: string; message: Pick<MessageOut, 'content' | 'attachments'> }) {
  const pictures = message.attachments.filter((attachment) => attachment.kind === 'image')
  const files = message.attachments.filter((attachment) => attachment.kind !== 'image')
  return (
    <UserBubble data-role="user">
      {message.content && <p className="whitespace-pre-wrap break-words">{message.content}</p>}
      {pictures.length > 0 && (
        <ul className="flex flex-wrap gap-2">
          {pictures.map((attachment) => (
            <li key={attachment.id} className="max-w-full">
              <AttachmentImage chatId={chatId} attachment={attachment} />
            </li>
          ))}
        </ul>
      )}
      {files.length > 0 && (
        <ul className="flex flex-wrap gap-1">
          {files.map((attachment) => (
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
 * trail, its text as Markdown, its result cards and what it says about itself
 * (its notices; an answer stored before notices has none). An answer with text
 * can be copied as its Markdown; the last answer offers to be regenerated
 * (`onRegenerate`), and, cut at the length limit, to go on (`onContinue`).
 */
export function MessageView({
  chatId,
  message,
  fileDays,
  onRegenerate,
  onContinue,
}: {
  chatId: string
  message: Pick<MessageOut, 'role' | 'content' | 'attachments' | 'steps' | 'results' | 'notices'>
  fileDays: number
  onRegenerate?: () => void
  onContinue?: () => void
}) {
  const t = useT()
  if (message.role === 'user') return <UserMessage chatId={chatId} message={message} />
  return (
    <div data-role="assistant" className="space-y-2">
      <Trail steps={message.steps} fileDays={fileDays} />
      {message.content && <MarkdownProse text={message.content} />}
      {message.results.map((result) => (
        <ResultCard key={result.id} chatId={chatId} result={result} />
      ))}
      <NoticeLines notices={message.notices ?? []} live={false} onContinue={onContinue} />
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

/** The answer being written: its steps as they run, its text so far, its cards, its notices as they come. */
export function DraftView({ chatId, draft, fileDays }: { chatId: string; draft: DraftAnswer; fileDays: number }) {
  return (
    <div data-role="assistant" aria-busy="true" className="space-y-2">
      <Trail steps={draft.steps} fileDays={fileDays} live />
      {draft.text ? <MarkdownProse text={draft.text} /> : !draft.error && <Pending reasoning={draft.reasoning} />}
      {draft.results.map((result) => (
        <ResultCard key={result.id} chatId={chatId} result={result} />
      ))}
      <NoticeLines notices={draft.notices} live />
    </div>
  )
}

/** The message being sent, with the files it binds (its pictures from the server's renditions,
 *  as the stored message's are), shown until the chat is read again. */
export function PendingUser({ chatId, message }: { chatId: string; message: PendingMessage }) {
  return <UserMessage chatId={chatId} message={message} />
}
