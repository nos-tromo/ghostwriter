"""The caps on one message; a round's prompt budget follows the model's context, 40k unless set."""

from typing import Final

# The model and its rounds.
#: The model's context, in tokens, while ``GHOSTWRITER_CONTEXT_TOKENS`` is unset.
CONTEXT_TOKENS: Final = 40_000
#: Each round's ``max_tokens``.
MAX_ANSWER_TOKENS: Final = 4_000
#: A reasoning round's ``max_tokens``: the thinking counts against it.
REASONING_ANSWER_TOKENS: Final = 12_000
#: Tokens of the context neither the prompt nor the answer may take: the estimate's error, the chat template.
CONTEXT_MARGIN_TOKENS: Final = 2_000
#: The least a reasoning round's prompt may hold, its tool schemas included.
MIN_PROMPT_TOKENS: Final = 8_000
#: The smallest context the backend starts with: a reasoning round's answer, the margin and that least prompt.
MIN_CONTEXT_TOKENS: Final = REASONING_ANSWER_TOKENS + CONTEXT_MARGIN_TOKENS + MIN_PROMPT_TOKENS


def prompt_tokens(context: int, *, reasoning: bool) -> int:
    """Return the most a round's prompt may hold, its tool schemas included.

    Args:
        context: The model's context, in tokens.
        reasoning: Whether the round reasons, and so may answer in ``REASONING_ANSWER_TOKENS``.

    Returns:
        The context less the round's answer and ``CONTEXT_MARGIN_TOKENS``.
    """
    answer = REASONING_ANSWER_TOKENS if reasoning else MAX_ANSWER_TOKENS
    return context - answer - CONTEXT_MARGIN_TOKENS


#: The most a round's prompt may hold at the default context.
PROMPT_TOKENS: Final = prompt_tokens(CONTEXT_TOKENS, reasoning=False)
#: The most a reasoning round's prompt may hold at the default context.
REASONING_PROMPT_TOKENS: Final = prompt_tokens(CONTEXT_TOKENS, reasoning=True)
#: Characters per token, the estimate.
CHARS_PER_TOKEN: Final = 3
#: Seconds each wait on the router may take (connecting, and each read of the stream); not a bound on the round.
MODEL_TIMEOUT_S: Final = 120.0

# Tools.
#: Rounds a message may offer tools in.
MAX_TOOL_ROUNDS: Final = 8
#: Calls a round may run.
MAX_CALLS_PER_ROUND: Final = 4
#: Seconds a message has for its tools.
MESSAGE_WALL_S: Final = 300.0
#: Seconds left of :data:`MESSAGE_WALL_S` below which a round is offered no tools.
TOOL_CUTOFF_S: Final = 10.0
#: Seconds before a call workbench answered busy is sent once more.
BUSY_RETRY_S: Final = 2.0

# What the model reads back.
#: Characters of a tool result the model reads.
TOOL_RESULT_CHARS: Final = 12_000
#: Characters of an earlier call's arguments replayed in a follow-up.
HISTORY_ARGUMENT_CHARS: Final = 2_000
#: Characters of an earlier call's result replayed in a follow-up.
HISTORY_RESULT_CHARS: Final = 2_000

# Result cards.
#: Cards a message may show.
MAX_RESULTS_PER_MESSAGE: Final = 20
#: The largest image shown inline.
INLINE_IMAGE_BYTES: Final = 10 * 1024 * 1024
#: The images shown inline.
IMAGE_MIMES: Final = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})

# Turns.
#: Worker threads the process's turns run on, apart from the shared pool; a turn beyond them waits.
TURN_THREADS: Final = 16
