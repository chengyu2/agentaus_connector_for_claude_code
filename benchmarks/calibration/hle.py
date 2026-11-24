"""The HLE-Diamond half: closed-book questions, one answer each, HLE's own format.

Text-only questions only: Agentaus cannot receive an image, so an image question would
measure the stripped prompt, not the model. Sampling is stratified across the
reasoning and knowledge partitions with a fixed seed, so every condition answers the
same questions.

Raw rows (question, answer, response) are HLE data under its canary and stay in
results/raw, which is gitignored.
"""
from __future__ import annotations

import hashlib
import json
import random
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import client, prompts
from .metrics import parse_answer, parse_confidence

MAX_TOKENS = 32_768          # cap per answer; a truncated answer is asked again (client.ask_final)


def load_items(parquet: str) -> list[dict]:
    import pandas as pd
    df = pd.read_parquet(parquet)
    df = df[df["image"].fillna("").astype(str).str.len() == 0]
    return [{"id": r.id, "question": r.question, "answer": r.answer,
             "answer_type": r.answer_type, "partition": r.partition, "category": r.category}
            for r in df.itertuples()]


def sample(items: list[dict], n: int, seed: int = 20261124) -> list[dict]:
    """n items, half from each partition, in a stable order. A larger n with the same
    seed starts with the same items, so a pilot is a prefix of the full run."""
    rng = random.Random(seed)
    by_part: dict[str, list] = {}
    for item in sorted(items, key=lambda i: i["id"]):
        by_part.setdefault(item["partition"], []).append(item)
    for part in by_part.values():
        rng.shuffle(part)
    parts = sorted(by_part)
    picked = []
    for i in range(n):
        part = by_part[parts[i % len(parts)]]
        picked.append(part[i // len(parts)])
    return picked


def answer(item: dict, condition: str) -> dict:
    messages = [{"role": "system", "content": prompts.system_prompt(condition, prompts.HLE_FORMAT)},
                {"role": "user", "content": item["question"]}]
    first = client.chat(messages, max_tokens=MAX_TOKENS)
    row = {"id": item["id"], "condition": condition, "partition": item["partition"],
           "answer_type": item["answer_type"], "category": item["category"],
           "truncated": first.truncated, "reasked": False,
           "input_tokens": first.input_tokens, "output_tokens": first.output_tokens,
           "cost_aud": first.cost_aud, "seconds": first.seconds,
           "first_response": first.content}
    final, row["final_path"] = first, "direct"
    if first.truncated or parse_confidence(first.content) is None:
        messages += [{"role": "assistant", "content": first.content or ""},
                     {"role": "user", "content": prompts.REASK}]
        replies, row["final_path"] = client.ask_final(messages)
        row["reasked"] = True
        for reply in replies:
            for key in ("input_tokens", "output_tokens", "seconds"):
                row[key] += getattr(reply, key)
            row["cost_aud"] += reply.cost_aud
        final = replies[-1]
    row["response"] = final.content
    row["model_answer"] = parse_answer(final.content)
    confidence = parse_confidence(final.content)
    row["confidence_missing"] = confidence is None
    row["confidence"] = 1.0 if confidence is None else confidence     # HLE: 100 if absent
    return row


def run(items: list[dict], conditions: list[str], out_dir: Path, *, workers: int = 4,
        repeat: int | dict = 1, seed: int = 20261124) -> None:
    """Answer every item under every condition, `repeat` times (or {condition: times}),
    skipping answers already on disk."""
    out_dir.mkdir(parents=True, exist_ok=True)
    jobs = []
    for condition in conditions:
        path = out_dir / f"hle_{condition}.jsonl"
        done = {}
        if path.exists():
            for line in path.read_text().splitlines():
                row = json.loads(line)
                done[(row["id"], row.get("repeat", 0))] = True
        times = repeat.get(condition, 1) if isinstance(repeat, dict) else repeat
        for r in range(times):
            jobs += [(item, condition, r, path) for item in items if (item["id"], r) not in done]
    # Interleave conditions, so time of day and API load are not confounded with condition.
    random.Random(seed).shuffle(jobs)

    def work(job):
        item, condition, r, path = job
        try:
            row = answer(item, condition)
        except Exception as exc:  # noqa: BLE001 - one failed item must not stop the run
            print(f"  {condition} {item['id']}: failed ({exc})", flush=True)
            return
        row["repeat"] = r
        with path.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        print(f"  {condition:<10} {item['id']} conf={row['confidence']:.2f} "
              f"out={row['output_tokens']} trunc={row['truncated']} {row['cost_aud']:.3f} AUD",
              flush=True)

    print(f"HLE: {len(jobs)} answers to get", flush=True)
    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(work, jobs))


def judge_key(condition: str, item_id: str, repeat: int) -> str:
    """An opaque key, so the judge cannot tell which prompt produced an answer."""
    return hashlib.sha256(f"{condition}|{item_id}|{repeat}".encode()).hexdigest()[:12]


def export_for_judge(items: list[dict], out_dir: Path, judge_file: Path) -> int:
    """One line per answer still to judge, holding only what HLE's judge sees, shuffled.

    Verdicts go to out_dir/hle_judgements.jsonl as {key, extracted_final_answer,
    reasoning, correct: yes|no}; keys map back through hle_judge_keys.json.
    """
    by_id = {i["id"]: i for i in items}
    verdicts = out_dir / "hle_judgements.jsonl"
    judged = ({json.loads(line)["key"] for line in verdicts.read_text().splitlines()}
              if verdicts.exists() else set())
    keymap_path = out_dir / "hle_judge_keys.json"
    keymap = json.loads(keymap_path.read_text()) if keymap_path.exists() else {}
    pending = []
    for path in sorted(out_dir.glob("hle_*.jsonl")):
        if path.name == "hle_judgements.jsonl":
            continue
        for line in path.read_text().splitlines():
            row = json.loads(line)
            if row["id"] not in by_id:
                continue
            key = judge_key(row["condition"], row["id"], row.get("repeat", 0))
            keymap[key] = [row["condition"], row["id"], row.get("repeat", 0)]
            if key not in judged:
                pending.append({"key": key, "question": by_id[row["id"]]["question"],
                                "correct_answer": by_id[row["id"]]["answer"],
                                "response": row["response"]})
    random.Random(0).shuffle(pending)
    keymap_path.write_text(json.dumps(keymap))
    with judge_file.open("w") as fh:
        for entry in pending:
            fh.write(json.dumps(entry) + "\n")
    return len(pending)
