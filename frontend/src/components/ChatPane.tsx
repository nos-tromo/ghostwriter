import { useState } from 'react'
import type { AppConfig } from '../api/types'
import { useAttachments } from '../hooks/useAttachments'
import { useChatStream } from '../hooks/useChatStream'
import { Composer } from './Composer'
import { Conversation } from './Conversation'

/**
 * The open chat: its one stream, the files its next message sends, and the
 * reasoning switch, shared by the conversation and the composer below it.
 *
 * Keyed by the chat's id where it is used, so nothing of it, the switch
 * included, carries over: a chat opens with reasoning off. Its answer lives
 * in the app's registry, so leaving the chat keeps it being written. Files
 * dropped on the conversation go to the same attachments as the paperclip's,
 * while attachments are on. `onNewChat` is the composer's offer of a fresh chat once
 * this one is nearly full.
 */
export function ChatPane({
  chatId,
  config,
  onNewChat,
}: {
  chatId: string
  config: AppConfig
  onNewChat: () => void
}) {
  const stream = useChatStream(chatId)
  const attachments = useAttachments(chatId, config)
  const [reasoning, setReasoning] = useState(false)
  return (
    <Conversation
      chatId={chatId}
      config={config}
      stream={stream}
      reasoning={reasoning}
      onDropFiles={config.attachments ? attachments.add : undefined}
      composer={
        <Composer
          chatId={chatId}
          config={config}
          stream={stream}
          attachments={attachments}
          reasoning={reasoning}
          onReasoningChange={setReasoning}
          onNewChat={onNewChat}
        />
      }
    />
  )
}
