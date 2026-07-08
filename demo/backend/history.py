"""History compaction: bound the model's context across long conversations and long turns.

pydantic-ai runs a ProcessHistory processor before *every* model request in the agent
loop, so a single processor covers both growth modes: turns accumulating across a
conversation, and tool call/return pairs accumulating inside one turn. When the pending
request is estimated to exceed a token budget, the oldest messages are summarized with the
demo's own model and the most recent messages are kept verbatim.

The budget is denominated in tokens (a fraction of the model's context window) but measured
by estimating the pending request's tokens directly (chars / CHARS_PER_TOKEN). RunContext
exposes `ctx.usage`, but that is cumulative for the whole run and reports prior steps, so it
lags the size of the request about to be sent -- and is zero on a turn's first request; the
direct estimate has neither problem.

The cut between "summarize" and "keep verbatim" is snapped to a boundary where no tool call
is still awaiting its return. A ToolReturnPart/RetryPromptPart sent to the model without its
originating ToolCallPart -- or a ToolCallPart with no return -- is a hard provider error, so
an unsafe cut would corrupt the request.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging

from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model

logger = logging.getLogger(__name__)

#: Chars-per-token divisor for the pending-request size estimate. Exact token counts vary by
#: tokenizer; the budget carries a safety fraction, so a coarse round divisor suffices.
CHARS_PER_TOKEN = 4

#: Most-recent messages always kept verbatim (never summarized).
KEEP_TAIL = 6

#: Prefix on the synthesized summary message, so it is recognizable in histories and traces.
SUMMARY_MARKER = "[summary of earlier conversation]"


def _estimate_tokens(messages: list[ModelMessage]) -> int:
    """Estimate the token cost of sending `messages`, from their text and tool-arg sizes."""
    chars = 0
    for message in messages:
        for part in message.parts:
            content = getattr(part, "content", None)
            if content is not None:
                chars += len(str(content))
            args = getattr(part, "args", None)
            if args is not None:
                chars += len(str(args))
    return chars // CHARS_PER_TOKEN


def _has_open_call(messages: list[ModelMessage], cut: int) -> bool:
    """Report whether `messages[:cut]` holds a tool call whose return lies at or after `cut`.

    Walk the head tracking tool_call_ids that were called but not yet answered; a
    ToolReturnPart or RetryPromptPart answers a call. A non-empty open set at the boundary
    means cutting there would orphan a call from its return.
    """
    open_ids: set[str] = set()
    for message in messages[:cut]:
        if isinstance(message, ModelResponse):
            for part in message.parts:
                if isinstance(part, ToolCallPart):
                    open_ids.add(part.tool_call_id)
        else:
            for part in message.parts:
                if isinstance(part, (ToolReturnPart, RetryPromptPart)):
                    open_ids.discard(part.tool_call_id)
    return bool(open_ids)


def _safe_cut(messages: list[ModelMessage], desired: int) -> int:
    """Largest index <= `desired` at which no tool call spans the cut (0 if none exists)."""
    for cut in range(min(desired, len(messages)), 0, -1):
        if not _has_open_call(messages, cut):
            return cut
    return 0


def build_compactor(
    model: Model,
    *,
    token_budget: int,
    keep_tail: int = KEEP_TAIL,
) -> Callable[[list[ModelMessage]], Awaitable[list[ModelMessage]]]:
    """Return a ProcessHistory processor that keeps the pending request under `token_budget`.

    `model` is the demo's own model, reused for summarization. The summarizer Agent has no
    tools and no capabilities, so it neither recurses through this processor nor emits extra
    instrumentation spans. `keep_tail` most-recent messages are always kept verbatim.

    When over budget this re-summarizes on each qualifying request; because each pass
    collapses the head to a single message, history usually drops back under budget
    immediately, so it is self-limiting. No running-summary cache is kept.
    """
    summarizer = Agent(
        model,
        instructions=(
            "Compress the earlier part of an assistant/tool conversation into a compact "
            "briefing. Preserve decisions, file paths, concrete values, and any unresolved "
            "threads; drop chatter. Write plain prose only -- never call tools."
        ),
    )

    async def compact(messages: list[ModelMessage]) -> list[ModelMessage]:
        estimate = _estimate_tokens(messages)
        if len(messages) <= keep_tail or estimate <= token_budget:
            return messages
        cut = _safe_cut(messages, len(messages) - keep_tail)
        if cut <= 0:
            # No safe boundary (e.g. one oversized, still-open turn); send unchanged rather
            # than risk orphaning a tool call.
            logger.info("history.compact.skipped estimate=%d messages=%d", estimate, len(messages))
            return messages
        head, tail = messages[:cut], messages[cut:]
        result = await summarizer.run("Summarize the conversation so far into a briefing.", message_history=head)
        summary = ModelRequest(parts=[UserPromptPart(content=f"{SUMMARY_MARKER}\n{result.output}")])
        logger.info(
            "history.compact estimate=%d budget=%d summarized=%d kept=%d",
            estimate,
            token_budget,
            len(head),
            len(tail),
        )
        return [summary, *tail]

    return compact
