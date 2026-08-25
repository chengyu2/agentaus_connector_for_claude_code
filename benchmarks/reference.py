"""Reference solutions, to prove each task's assertions are satisfiable.

If a task's tests are unsatisfiable both arms fail it and the benchmark learns nothing,
so every task must be shown to be passable before it is used to judge anything.
"""
REFS = {
"median": '''
def median(nums):
    if not nums: raise ValueError("empty")
    s = sorted(nums); n = len(s)
    return s[n//2] if n % 2 else (s[n//2 - 1] + s[n//2]) / 2
''',
"chunk": '''
def chunk(items, size):
    if size < 1: raise ValueError("size must be >= 1")
    return [items[i:i+size] for i in range(0, len(items), size)]
''',
"roman": '''
def to_roman(n):
    if not isinstance(n, int) or n < 1 or n > 3999: raise ValueError("out of range")
    vals = [(1000,"M"),(900,"CM"),(500,"D"),(400,"CD"),(100,"C"),(90,"XC"),
            (50,"L"),(40,"XL"),(10,"X"),(9,"IX"),(5,"V"),(4,"IV"),(1,"I")]
    out = []
    for v, s in vals:
        while n >= v: out.append(s); n -= v
    return "".join(out)
''',
"merge_intervals": '''
def merge(intervals):
    if not intervals: return []
    s = sorted(intervals, key=lambda x: x[0]); out = [list(s[0])]
    for a, b in s[1:]:
        if a <= out[-1][1]: out[-1][1] = max(out[-1][1], b)
        else: out.append([a, b])
    return out
''',
"word_count": '''
def word_count(text):
    import re
    out = {}
    for w in re.findall(r"[\\w']+", text.lower()):
        w = w.strip("'")
        if w: out[w] = out.get(w, 0) + 1
    return out
''',
"flatten": '''
def flatten(nested):
    out = []
    for x in nested:
        if isinstance(x, list): out.extend(flatten(x))
        else: out.append(x)
    return out
''',
"parse_version": '''
def compare_versions(a, b):
    pa = [int(x) for x in a.split(".")]; pb = [int(x) for x in b.split(".")]
    n = max(len(pa), len(pb))
    pa += [0]*(n-len(pa)); pb += [0]*(n-len(pb))
    return (pa > pb) - (pa < pb)
''',
"retry_backoff": '''
def backoff_delays(attempts, base, cap):
    if base <= 0: raise ValueError("base must be > 0")
    if attempts <= 0: return []
    return [min(base * 2**i, cap) for i in range(attempts)]
''',
"add_months": '''
def add_months(year, month, day, n):
    total = year*12 + (month-1) + n
    y, m = total//12, total % 12 + 1
    leap = (y % 4 == 0 and y % 100 != 0) or y % 400 == 0
    dim = [31, 29 if leap else 28, 31,30,31,30,31,31,30,31,30,31][m-1]
    return (y, m, min(day, dim))
''',
"parse_csv_line": '''
def parse_csv_line(line):
    fields, cur, i, inq = [], [], 0, False
    while i < len(line):
        c = line[i]
        if inq:
            if c == '"':
                if i+1 < len(line) and line[i+1] == '"': cur.append('"'); i += 2; continue
                inq = False; i += 1; continue
            cur.append(c); i += 1
        else:
            if c == '"': inq = True; i += 1
            elif c == ',': fields.append("".join(cur)); cur = []; i += 1
            else: cur.append(c); i += 1
    fields.append("".join(cur))
    return fields
''',
"toposort": '''
def toposort(graph):
    state, out = {}, []
    def visit(n):
        if state.get(n) == 2: return
        if state.get(n) == 1: raise ValueError("cycle")
        state[n] = 1
        for d in graph.get(n, []): visit(d)
        state[n] = 2; out.append(n)
    for n in list(graph): visit(n)
    return out
''',
"lru": '''
class LRU:
    def __init__(self, capacity):
        if capacity < 1: raise ValueError("capacity must be >= 1")
        from collections import OrderedDict
        self.cap = capacity; self.d = OrderedDict()
    def get(self, key):
        if key not in self.d: return None
        self.d.move_to_end(key); return self.d[key]
    def put(self, key, value):
        if key in self.d: self.d.move_to_end(key)
        self.d[key] = value
        if len(self.d) > self.cap: self.d.popitem(last=False)
''',
"running_median": '''
def running_stats(nums):
    out, total, mn, mx = [], 0, None, None
    for i, x in enumerate(nums, 1):
        total += x
        mn = x if mn is None else min(mn, x)
        mx = x if mx is None else max(mx, x)
        out.append((i, total/i, mn, mx))
    return out
''',
"glob_match": '''
def matches(pattern, text):
    m, n = len(pattern), len(text)
    dp = [[False]*(n+1) for _ in range(m+1)]
    dp[0][0] = True
    for i in range(1, m+1):
        if pattern[i-1] == '*': dp[i][0] = dp[i-1][0]
    for i in range(1, m+1):
        for j in range(1, n+1):
            if pattern[i-1] == '*': dp[i][j] = dp[i-1][j] or dp[i][j-1]
            elif pattern[i-1] == '?' or pattern[i-1] == text[j-1]: dp[i][j] = dp[i-1][j-1]
    return dp[m][n]
''',
}
