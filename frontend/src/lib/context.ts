import type { AppConfig } from '../api/types'

/** From this share of the budget a chat is nearly full: the ring turns to the warning colour and the note
 *  suggests a new chat. A frontend constant. */
export const NEAR_FULL_RATIO = 0.8

/**
 * How much of the prompt budget the chat's latest answer says the next prompt takes.
 *
 * @param estimate - The answer's `context_estimate` in tokens, or null where none was made.
 * @param config - The deployment's budgets.
 * @param reasoning - Whether the next message thinks first, which has the smaller budget.
 * @returns The share of the budget, capped at 1 and never rounded; null without an estimate, or without a
 *   budget to divide by.
 */
export function contextRatio(estimate: number | null, config: AppConfig, reasoning: boolean): number | null {
  if (estimate === null) return null
  const budget = reasoning ? config.reasoning_prompt_tokens : config.prompt_tokens
  if (!(budget > 0)) return null
  return Math.min(1, estimate / budget)
}

/**
 * Whether a chat is nearly full. Decided on the ratio as the ring gets it, never on its rounded percent, so
 * the ring and the note always agree: 0.799 reads as 80 % and is not yet nearly full.
 *
 * @param ratio - The {@link contextRatio}.
 * @returns Whether it is at {@link NEAR_FULL_RATIO} or above.
 */
export function nearlyFull(ratio: number | null): boolean {
  return ratio !== null && ratio >= NEAR_FULL_RATIO
}
