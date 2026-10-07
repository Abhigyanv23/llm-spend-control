"""Prompt fingerprints: group "the same kind of prompt" without storing the prompt.

    "Summarise ticket #4521 for Alice"  ┐
    "Summarise ticket #87 for Alice"    ┘ -> same fingerprint (digits normalised)

The fingerprint is a SHA-256 of the normalised instruction text (system prompts + latest user
message, lower-cased, digits replaced, whitespace collapsed). It is one-way: you can group,
count and cost prompt patterns, but you can't read them back. That's data minimisation applied
to analytics. A short preview is stored only when privacy settings allow it.
"""
import hashlib
import re

from app.schemas import ChatRequest

DIGITS = re.compile(r"\d+")
WHITESPACE = re.compile(r"\s+")


def instruction_text(request: ChatRequest) -> str:
    """System prompts + the latest user message: the task (same rule as the classifier)."""
    system = [m.content for m in request.messages if m.role == "system"]
    last_user = next((m.content for m in reversed(request.messages) if m.role == "user"), "")
    return "\n".join([*system, last_user])


def normalise(text: str) -> str:
    return WHITESPACE.sub(" ", DIGITS.sub("0", text.lower())).strip()


def fingerprint_text(text: str) -> str:
    return hashlib.sha256(normalise(text).encode("utf-8")).hexdigest()


def prompt_fingerprint(request: ChatRequest) -> str:
    return fingerprint_text(instruction_text(request))


def prompt_preview(request: ChatRequest, chars: int) -> str:
    """First `chars` characters of the instruction text, whitespace-collapsed."""
    return WHITESPACE.sub(" ", instruction_text(request)).strip()[:chars].rstrip()
