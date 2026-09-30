"""Token estimation used BEFORE a provider call (context check, budget reservation).

This is a heuristic (~4 characters per token for English). Real tokenizers differ per
model, so the estimate can be off for code or non-English text. Phase 3 can swap in a
real tokenizer without changing callers.
"""
from collections.abc import Iterable

from app.schemas import Message

# Chat formats wrap every message in role markers / separators that cost a few tokens
MESSAGE_OVERHEAD_TOKENS = 4


def estimate_tokens(text: str) -> int:
    """Rough heuristic: ~4 characters per token for English text."""
    return max(1, len(text) // 4)


def estimate_input_tokens(messages: Iterable[Message]) -> int:
    """Conservative input estimate: per-message text estimate + per-message overhead."""
    return sum(estimate_tokens(m.content) + MESSAGE_OVERHEAD_TOKENS for m in messages)
