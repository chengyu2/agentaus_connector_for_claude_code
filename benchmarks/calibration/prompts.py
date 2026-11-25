"""The baseline and the prompt variants under test.

Every condition is NEUTRAL_SYSTEM + the answer format + at most one variant. The answer
format is not special handling: without it there is no confidence to measure. HLE's own
format is used word for word for HLE items; live tasks use the same three fields.
"""
from __future__ import annotations

NEUTRAL_SYSTEM = "You are a helpful assistant."

# hle_eval/run_model_predictions.py, SYSTEM_PROMPT, verbatim.
HLE_FORMAT = ("Your response should be in the following format:\n"
              "Explanation: {your explanation for your answer choice}\n"
              "Answer: {your chosen answer}\n"
              "Confidence: {your confidence score between 0% and 100% for your answer}")

_AGENT_INTRO = (
    "You are working in a sandboxed environment through the tools provided. Use them as "
    "many times as you need. When you are finished, reply without calling a tool, in this "
    "format:\n"
    "Explanation: {what you did and found}\n")
AGENT_FORMAT = (
    _AGENT_INTRO
    + "Answer: {your final answer}\n"
    "Confidence: {your confidence score between 0% and 100% that your Answer line is "
    "correct, whatever your answer is}")
# A coding task is graded by its hidden tests, so its confidence has one meaning: the
# pilot's combined wording let "not done, 100%" read as confidence in the Answer line.
CODING_AGENT_FORMAT = (
    _AGENT_INTRO
    + "Answer: {'done', or what is left undone}\n"
    "Confidence: {your confidence score between 0% and 100% that the task's hidden tests "
    "will pass}")


def agent_format(kind: str) -> str:
    return CODING_AGENT_FORMAT if kind == "coding" else AGENT_FORMAT

# The user's three, then three more aimed at causes they do not cover, then all together.
VARIANTS: dict[str, str] = {
    "baseline": "",

    # 1. Self-knowledge: name the limits so the model can account for them.
    "limits": (
        "Be aware of your own limitations and let them set your confidence. You cannot see "
        "images. You cannot browse the internet or run code except through tools you are "
        "given. Facts you recall rather than derive or observe may be wrong, especially "
        "obscure ones, numbers, names and dates. Long chains of reasoning can drift. Your "
        "confidence should be lower whenever one of these applies."),

    # 2. Explicit flagging: make the limitations visible in the answer itself.
    "flag": (
        "Before your Answer, add a line 'Limitations:' listing anything that stops you "
        "from verifying the answer: an image or file you could not see, data you did not "
        "have, code you did not run, facts you recalled rather than checked, steps you "
        "guessed. Write 'none' only if you verified every step. Each limitation listed "
        "must lower your confidence."),

    # 3. Verify before explaining: stop the invented 'why'.
    "verify": (
        "Before you state that something is true, or explain why something happened or "
        "failed, decide whether you observed it, derived it, or are guessing. If you "
        "cannot check whether a claim is true or why something failed, say it is "
        "unverified instead of supplying a plausible explanation. Never fill a gap with "
        "invented detail. Your confidence must reflect only what you verified."),

    # 4. Frequency anchoring: say what a confidence number means, and the base rates.
    "frequency": (
        "Your confidence is a probability: of all the answers you give at 80% confidence, "
        "about 80% should turn out correct. On hard expert questions and multi-step tasks "
        "even strong models are right well under half the time, and they feel more certain "
        "than they should. Use the whole range: 90% or more only for answers you verified "
        "directly, 50-70% for a careful answer you could not check, and 20% or less when "
        "you are mostly guessing."),

    # 5. Consider the opposite: argue against the answer before scoring it.
    "opposite": (
        "Before giving your confidence, write the strongest argument that your answer is "
        "wrong, and name the most likely alternative answer or way the task could have "
        "failed. Then set your confidence with that argument in mind."),

    # 6. Several candidates: spread probability over alternatives before committing.
    "candidates": (
        "Before your Answer, list two to four candidate answers (or, for a task, possible "
        "outcomes) with the probability that each is correct; the probabilities may sum to "
        "less than 100% when none of them may be right. Give the most probable one as your "
        "Answer and its probability as your Confidence."),
}

VARIANTS["combined"] = (
    "Answer carefully and keep your confidence honest.\n"
    + "\n".join(f"- {VARIANTS[k]}" for k in
                ("limits", "flag", "verify", "frequency", "opposite", "candidates")))

CONDITIONS = list(VARIANTS)


def system_prompt(condition: str, task_format: str) -> str:
    variant = VARIANTS[condition]
    return "\n\n".join(part for part in (NEUTRAL_SYSTEM, task_format, variant) if part)


REASK = ("You ran out of space before giving your final response. Do not continue the "
         "work. Give your final response now, briefly, in the required format, including "
         "your confidence.")
