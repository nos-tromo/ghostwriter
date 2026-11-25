import { useCallback, useSyncExternalStore } from 'react'
import type { AttachmentOut, MessageIn, MessageOut, RegenerateIn } from '../api/types'
import { IDLE, useChatStreams, type PendingMessage, type StreamState } from './chatStreams'

export type { DraftAnswer, PendingMessage, StreamFailure } from './chatStreams'

export interface ChatStream extends StreamState {
  /** Send a message with the chat's pending attachments it binds (sent by their ids).
   *  Resolves false when the server refused it with an error status, so it was
   *  not stored, or while the chat still streams; true once it was taken, also
   *  when its answer ended early. */
  send(content: string, attachments: readonly AttachmentOut[], reasoning: boolean): Promise<boolean>
  regenerate(reasoning: boolean): Promise<void>
  /** Go on with `answer`, the chat's latest, cut at the length limit: its text goes on in the
   *  same answer. The server refuses another answer, with the code `not_continuable`. */
  continueAnswer(answer: MessageOut): Promise<void>
  stop(): Promise<void>
}

const NO_CHAT = () => () => {}

/**
 * A chat's answer as it streams: send a message or regenerate the last answer,
 * watch its draft, and stop it.
 *
 * The stream itself lives in the app's registry (`ChatStreamsProvider`), one
 * per chat, so this is only its view: leaving the chat keeps the answer being
 * written, and returning shows its draft.
 *
 * @param chatId - The chat, or null for none.
 * @returns The stream's state and its actions.
 */
export function useChatStream(chatId: string | null): ChatStream {
  const streams = useChatStreams()
  const subscribe = useCallback(
    (listener: () => void) => (chatId === null ? NO_CHAT() : streams.subscribe(chatId, listener)),
    [streams, chatId],
  )
  const state = useSyncExternalStore(subscribe, () => (chatId === null ? null : streams.get(chatId))) ?? IDLE

  const run = useCallback(
    async (
      route: 'messages' | 'regenerate' | 'continue',
      body: MessageIn | RegenerateIn | Record<string, never>,
      pendingUser: PendingMessage | null,
      base?: MessageOut,
    ) => (chatId === null ? false : ((await streams.start(chatId, route, body, pendingUser, base)) ?? false)),
    [streams, chatId],
  )

  const send = useCallback(
    (content: string, attachments: readonly AttachmentOut[], reasoning: boolean) =>
      run(
        'messages',
        { content, attachment_ids: attachments.map((a) => a.id), reasoning } satisfies MessageIn,
        { content, attachments: [...attachments] },
      ),
    [run],
  )

  const regenerate = useCallback(
    async (reasoning: boolean) => {
      await run('regenerate', { reasoning } satisfies RegenerateIn, null)
    },
    [run],
  )

  const continueAnswer = useCallback(
    async (answer: MessageOut) => {
      await run('continue', {}, null, answer)
    },
    [run],
  )

  const stop = useCallback(async () => {
    if (chatId !== null) await streams.stop(chatId)
  }, [streams, chatId])

  return { ...state, send, regenerate, continueAnswer, stop }
}
