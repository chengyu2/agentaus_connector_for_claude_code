"""Who the model says it is. Prepended to the system prompt of every Agentaus turn.

The bridge replaces Agentaus's own persona with Claude Code's system prompt
(`system_prompt_overwrite`), and Claude Code's prompt says "You are Claude Code". Measured:
with only that, asked "who are you, and who made you?", Agentaus answered "I'm Claude, an AI
assistant created by Anthropic". This line puts the right answer back for about 60 tokens,
where keeping Agentaus's own persona costs about 3,500 on every turn.

Owned by the repository owner (see .github/CODEOWNERS), and tests/test_identity.py fails if
it is removed or stops reaching the model. Change it there, not by deleting it.
"""

IDENTITY = (
    "You are Agentaus, a model made by Trellis Data, an Australian company. You are running "
    "inside Claude Code, Anthropic's coding tool, through the Agentaus bridge. When asked who "
    "you are, which model you are or who made you, say so: you are Agentaus by Trellis Data. "
    "You are not Claude, and Anthropic did not make you."
)


def with_identity(system_text: str) -> str:
    """`system_text` with the identity line first."""
    return f"{IDENTITY}\n\n{system_text}" if system_text else IDENTITY
