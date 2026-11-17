import { useCallback, useEffect, useLayoutEffect, useRef, useState, type ClipboardEvent, type KeyboardEvent } from 'react'
import {
  AttachButton,
  BrainActiveIcon,
  BrainIcon,
  Button,
  ChatComposer,
  MicButton,
  SendButton,
  StopButton,
} from '@infra/ui'
import { transcribe } from '../api/chats'
import type { AppConfig } from '../api/types'
import { isAttachable, type Attachments } from '../hooks/useAttachments'
import type { ChatStream } from '../hooks/useChatStream'
import { useRecorder } from '../hooks/useRecorder'
import { useT } from '../i18n/LanguageContext'
import { AttachmentChips } from './AttachmentChips'
import { RequestError } from './RequestError'

/**
 * Whether the model thinks before it answers, as a single icon button
 * (docint's, from `ChatControls.tsx`, with its state held by the chat).
 *
 * Reasoning buys answer quality with latency and tokens, so it is off until
 * asked for and flipped per chat rather than per deployment. It carries no
 * label, so the state lives in the accessible name and the drawing itself
 * changes: the brain lights up rather than merely taking a pressed tint (the
 * `BrainIcon`/`BrainActiveIcon` state pair from `@infra/ui`).
 */
function ReasoningToggle({ reasoning, onChange }: { reasoning: boolean; onChange: (next: boolean) => void }) {
  const t = useT()
  const name = t('chat.reasoning_state', {
    mode: reasoning ? t('chat.reasoning_on') : t('chat.reasoning_off'),
  })
  return (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      aria-pressed={reasoning}
      aria-label={name}
      title={name}
      onClick={() => onChange(!reasoning)}
      className="h-8 w-8 shrink-0 px-0"
    >
      {reasoning ? <BrainActiveIcon className="h-4 w-4" /> : <BrainIcon className="h-4 w-4" />}
    </Button>
  )
}

/** `text` with `words` put in place of the selection, a space kept on either side where words meet. */
function spliceWords(text: string, start: number, end: number, words: string): { text: string; caret: number } {
  const before = text.slice(0, start)
  const after = text.slice(end)
  const lead = before && !/\s$/.test(before) ? ' ' : ''
  const trail = after && !/^\s/.test(after) ? ' ' : ''
  const head = `${before}${lead}${words}${trail}`
  return { text: `${head}${after}`, caret: head.length }
}

/**
 * The message being written, and what goes with it: the files attached by
 * the paperclip, a paste or a drop on the conversation; dictation into the
 * text; the reasoning switch; send, or stop while an answer streams.
 *
 * Enter sends and Shift+Enter starts a new line. Send waits while a file
 * uploads, and needs text or an attached file. A transcript goes in at the
 * cursor and is never sent by itself; one still running when the composer
 * goes is aborted. A message the server refuses comes back into the composer
 * with its files, as it was.
 *
 * The chat's stream, its attachments and the reasoning switch are the
 * `ChatPane`'s, so the conversation above shows the same answer this sends.
 */
export function Composer({
  config,
  stream,
  attachments,
  reasoning,
  onReasoningChange,
}: {
  config: AppConfig
  stream: ChatStream
  attachments: Attachments
  reasoning: boolean
  onReasoningChange: (next: boolean) => void
}) {
  const t = useT()
  const [text, setText] = useState('')
  const textRef = useRef<HTMLTextAreaElement>(null)
  const pickerRef = useRef<HTMLInputElement>(null)
  const caretRef = useRef<number | null>(null)
  const [transcribing, setTranscribing] = useState(false)
  const [dictationFailure, setDictationFailure] = useState<unknown>(null)
  const transcriptionRef = useRef<AbortController | null>(null)

  useEffect(() => () => transcriptionRef.current?.abort(), [])

  // Put the cursor after a transcript once it is in the textarea.
  useLayoutEffect(() => {
    const caret = caretRef.current
    if (caret === null || !textRef.current) return
    caretRef.current = null
    textRef.current.focus()
    textRef.current.setSelectionRange(caret, caret)
  }, [text])

  const insert = useCallback((transcript: string) => {
    const words = transcript.trim()
    const area = textRef.current
    if (!words || !area) return
    const next = spliceWords(area.value, area.selectionStart, area.selectionEnd, words)
    caretRef.current = next.caret
    setText(next.text)
  }, [])

  const onRecorded = useCallback(
    async (blob: Blob, mime: string) => {
      const controller = new AbortController()
      transcriptionRef.current = controller
      setTranscribing(true)
      setDictationFailure(null)
      try {
        insert(await transcribe(blob, mime, controller.signal))
      } catch (error) {
        if (!controller.signal.aborted) setDictationFailure(error)
      } finally {
        if (transcriptionRef.current === controller) transcriptionRef.current = null
        setTranscribing(false)
      }
    },
    [insert],
  )
  const recorder = useRecorder({
    maxSeconds: config.max_recording_s,
    onRecorded: (blob, mime) => void onRecorded(blob, mime),
  })

  const done = attachments.pending.filter((f) => f.status === 'done' && f.attachment)
  const uploading = attachments.pending.some((f) => f.status === 'uploading')
  const content = text.trim()
  const canSend = !stream.streaming && !uploading && (content !== '' || done.length > 0)

  const submit = async () => {
    if (!canSend) return
    const written = text
    const files = attachments.clear()
    setText('')
    const taken = await stream.send(
      content,
      files.flatMap((f) => (f.attachment ? [f.attachment] : [])),
      reasoning,
    )
    if (taken) return
    setText((current) => (current === '' ? written : current))
    attachments.restore(files)
  }

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== 'Enter' || event.shiftKey || event.nativeEvent.isComposing) return
    event.preventDefault()
    void submit()
  }

  // A paste of files attaches them. Text is pasted as ever; what comes with
  // it is a picture of that text (an office program's copy) unless it is of
  // a type that can be attached. Text that is just a pasted file's name (a
  // file manager's copy) is that file, not text to paste.
  const onPaste = (event: ClipboardEvent<HTMLTextAreaElement>) => {
    if (!config.attachments) return
    const files = Array.from(event.clipboardData.files)
    if (files.length === 0) return
    const pasted = event.clipboardData.getData('text/plain')
    if (pasted && !files.some((f) => f.name === pasted)) {
      const attachable = files.filter((f) => isAttachable(f.name, config))
      if (attachable.length > 0) attachments.add(attachable)
      return
    }
    event.preventDefault()
    attachments.add(files)
  }

  const recording = recorder.state === 'recording'
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault()
        void submit()
      }}
      className="shrink-0 py-2"
    >
      {/* As wide as the message column (Conversation), whose padding is inside its max width too. */}
      <div className="mx-auto w-full max-w-3xl space-y-2 px-4">
        <AttachmentChips pending={attachments.pending} onDismiss={attachments.dismiss} />
        {attachments.pending.some((f) => f.status !== 'error') && (
          <p className="text-xs text-muted-foreground">
            {t('composer.files_stay', { days: config.file_retention_days })}
          </p>
        )}
        <ChatComposer
          ref={textRef}
          value={text}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={onKeyDown}
          onPaste={onPaste}
          aria-label={t('composer.placeholder')}
          placeholder={t('composer.placeholder')}
          start={
            config.attachments || config.speech_input ? (
              <>
                {config.attachments && (
                  <>
                    <AttachButton label={t('composer.attach')} onClick={() => pickerRef.current?.click()} />
                    <input
                      ref={pickerRef}
                      type="file"
                      multiple
                      hidden
                      accept={config.attachment_types.join(',')}
                      onChange={(event) => {
                        attachments.add(Array.from(event.target.files ?? []))
                        event.target.value = ''
                      }}
                    />
                  </>
                )}
                {config.speech_input &&
                  (recording ? (
                    <StopButton label={t('mic.stop')} onClick={recorder.stop} />
                  ) : (
                    <MicButton
                      label={t('mic.start')}
                      busy={transcribing}
                      disabled={recorder.state === 'unsupported'}
                      hint={recorder.state === 'unsupported' ? t('mic.unsupported') : undefined}
                      onClick={() => void recorder.start()}
                    />
                  ))}
                {/* A ticking count, deliberately not a live region. */}
                {recording && (
                  <span className="text-xs tabular-nums text-muted-foreground">
                    {t('mic.limit', { s: Math.max(0, config.max_recording_s - recorder.elapsedS) })}
                  </span>
                )}
              </>
            ) : undefined
          }
          end={
            <>
              <ReasoningToggle reasoning={reasoning} onChange={onReasoningChange} />
              {stream.streaming ? (
                <StopButton label={t('composer.stop')} onClick={() => void stream.stop()} />
              ) : (
                <SendButton type="submit" variant="primary" label={t('composer.send')} disabled={!canSend} />
              )}
            </>
          }
        />
        {(recorder.state === 'denied' || recorder.state === 'unavailable') && (
          <p role="alert" className="text-xs text-danger">
            {t(recorder.state === 'denied' ? 'mic.denied' : 'mic.unavailable')}
          </p>
        )}
        {dictationFailure !== null && <RequestError error={dictationFailure} />}
      </div>
    </form>
  )
}
