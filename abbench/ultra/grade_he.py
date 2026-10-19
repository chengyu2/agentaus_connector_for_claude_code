"""Grade `he10`: each function on its original tests and on the edge inputs, against the
canonical solution. Usage: grade_he.py <workdir> <he10_key.json>"""
import copy, json, math, multiprocessing as mp, sys

def check(item, path, q):
    try:
        ns = {}
        exec(compile(open(path).read(), "solutions.py", "exec"), ns)
        cand = ns[item["entry"]]
        ref_ns = {}
        exec(item["prompt"] + item["canonical"], ref_ns)
        ref = ref_ns[item["entry"]]
        tns = dict(ns)
        exec(item["test"], tns)
        tns["check"](cand)
        for args in item["edge"]:
            args = [tuple(a) if isinstance(a, list) and item["entry"] == "intersection" else a for a in args]
            want = ref(*copy.deepcopy(args))
            got = cand(*copy.deepcopy(args))
            if isinstance(want, float) or isinstance(got, float):
                if not (isinstance(got, (int, float)) and math.isclose(want, got, rel_tol=1e-6)):
                    q.put(f"edge {args!r}: got {got!r}, want {want!r}"); return
            elif got != want:
                q.put(f"edge {args!r}: got {got!r}, want {want!r}"); return
        q.put("ok")
    except BaseException as e:
        q.put(f"error {e!r}"[:200])

def main():
    wd, keyp = sys.argv[1], sys.argv[2]
    out, passed = {}, 0
    for item in json.load(open(keyp)):
        q = mp.Queue()
        p = mp.Process(target=check, args=(item, f"{wd}/solutions.py", q))
        p.start(); p.join(30)
        if p.is_alive():
            p.kill(); r = "timeout"
        else:
            r = q.get() if not q.empty() else "crashed"
        out[item["id"]] = r
        passed += r == "ok"
    out["score"] = round(passed / len(out), 3)
    print(json.dumps(out))

if __name__ == "__main__":
    main()
