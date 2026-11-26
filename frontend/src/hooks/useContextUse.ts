import type { AppConfig, MessageOut } from '../api/types'
import { contextRatio } from '../lib/context'
import type { ChatStream } from './useChatStream'
import { useChat } from './useChats'

/** Whether an answer was written with the context trimmed (earlier turns left out of the prompt). */
function wasTrimmed(notices: readonly { code: string }[] | null | undefined): boolean {
  return notices?.some((notice) => notice.code === 'context_trimmed') ?? false
}

/**
 * How full the chat's context is, and whether the model no longer gets all of it.
 *
 * The ratio reads the latest stored answer's `context_estimate` (the estimated size of the next prompt)
 * against the budget of the reasoning switch as it is now. While an answer is written, its own row is left
 * out as long as it has no estimate yet (a new answer's row), so the ring stays at the last answer's reading
 * until the stored one replaces it. A row that has one is kept: a continued answer is the cut answer itself,
 * which holds its reading already. An answer stored without an estimate (written before estimates were kept,
 * or stopped early) gives none.
 *
 * `trimmed` says the latest answer's prompt left earlier turns out: while it streams, its draft's notice;
 * otherwise the latest stored answer's.
 *
 * @param chatId - The chat.
 * @param stream - The chat's stream.
 * @param config - The deployment's budgets.
 * @param reasoning - Whether the next message thinks first.
 * @returns The ratio (not rounded, capped at 1; null for none) and whether the context was trimmed.
 */
export function useContextUse(
  chatId: string,
  stream: ChatStream,
  config: AppConfig,
  reasoning: boolean,
): { ratio: number | null; trimmed: boolean } {
  const { data: chat } = useChat(chatId)
  const writing = stream.streaming ? stream.stored?.answerId : undefined
  const answers: MessageOut[] = (chat?.messages ?? []).filter(
    (m) => m.role === 'assistant' && !(m.id === writing && m.context_estimate === null),
  )
  const latest = answers.at(-1)
  const ratio = contextRatio(latest?.context_estimate ?? null, config, reasoning)
  const trimmed = stream.streaming && stream.draft ? wasTrimmed(stream.draft.notices) : wasTrimmed(latest?.notices)
  return { ratio, trimmed }
}
