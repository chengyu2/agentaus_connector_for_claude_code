"""Build the `he10` task: ten HumanEval functions in one file, graded strictly.

The ten are the ones a frontier model or Agentaus got wrong in this repository's earlier
A/B (on HumanEval+), so the task has headroom for both arms. The original tests are
weak - HumanEval+ exists because of that - so each function is also checked on edge
inputs written here, against the canonical solution, the way HumanEval+ does it.

    python build_he.py <HumanEval.jsonl.gz> <out_dir>
"""
import gzip
import json
import sys

PICK = [91, 99, 103, 123, 125, 127, 141, 145, 151, 163]

# Inputs where the usual wrong readings of each docstring diverge from the reference.
EDGE = {
    91: [("I love it.",), ("Hello world",), ("I am bored! I am tired? Is it? I",), ("i like it. I",),
         ("The sky. I think. Ice cream.",), ("",), ("  I wonder.  I do",), ("I.I.I",)],
    99: [("14.5",), ("-14.5",), ("0.5",), ("-0.5",), ("15.3",), ("10",), ("-15.5",), ("0",), ("2.4999",)],
    103: [(1, 5), (7, 5), (10, 20), (20, 33), (7, 7), (1, 2), (964, 977), (185, 546)],
    123: [(1,), (5,), (14,), (27,), (2,), (3,)],
    125: [("Hello world!",), ("Hello,world!",), ("abcdef",), ("aaaBb",), ("",), ("a,b c",), ("ABC",), ("zz yy",)],
    127: [((1, 2), (2, 3)), ((-1, 1), (0, 4)), ((-3, -1), (-5, 5)), ((-2, 2), (-4, 0)), ((1, 1), (1, 1)),
          ((-11, 2), (-1, -1)), ((1, 2), (3, 5)), ((-6, 2), (-4, 5))],
    141: [("example.txt",), ("1example.dll",), ("s1sdf3.asd",), ("K.dll",), ("MY16FILE3.exe",), ("His12FILE94.exe",),
          ("_Y.txt",), ("?aREYA.exe",), ("/this_is_valid.dll",), ("this_is_valid.wow",), ("this_is_valid.txt",),
          ("this_is_valid.txtexe",), ("#this2_i4s_5valid.ten",), ("@this1_is6_valid.exe",), ("this_is_12valid.6exe4.txt",),
          ("all.exe.txt",), ("I563_No.exe",), ("Is3youfault.txt",), ("no_one#knows.dll",), ("1I563_Yes3.exe",),
          ("I563_Yes3.txtt",), ("final..txt",), ("final132",), ("_f4indsartal132.",), (".txt",), ("s.",), ("a1234.txt",)],
    145: [([1, 11, -1, -11, -12],), ([],), ([1234, 423, 463, 145, 2, 423, 423, 53, 6, 37, 3457, 3, 56, 0, 46],),
          ([1, -11, -32, 43, 54, -98, 2, -3],), ([0, 6, 6, -76, -21, 23, 4],), ([-1, -10, 10, 1],)],
    151: [([1, 3, 2, 0],), ([-1, -2, 0],), ([9, -2],), ([0],), ([],), ([5.0, 3],), ([0.1, 0.2, 0.3],), ([-10, -20, -30],),
          ([-1.0, 1.0, 3.0],), ([7, 2.5, 3],)],
    163: [(2, 10), (10, 2), (132, 2), (17, 89), (2, 8), (8, 2), (1, 1), (5, 5), (6, 6)],
}


def main():
    src, out = sys.argv[1], sys.argv[2]
    problems = {int(p["task_id"].split("/")[1]): p for p in
                (json.loads(line) for line in gzip.open(src, "rt"))}
    chosen = [problems[n] for n in PICK]
    stubs = ["\n\n".join(p["prompt"].rstrip() + "\n    raise NotImplementedError\n" for p in chosen)]
    open(f"{out}/solutions.py", "w").write("\n\n".join(stubs))
    key = [{"id": p["task_id"], "entry": p["entry_point"], "prompt": p["prompt"],
            "canonical": p["canonical_solution"], "test": p["test"],
            "edge": [list(a) for a in EDGE[int(p["task_id"].split("/")[1])]]} for p in chosen]
    json.dump(key, open(f"{out}/../he10_key.json", "w"))
    print(f"{len(chosen)} problems -> {out}/solutions.py")


if __name__ == "__main__":
    main()
