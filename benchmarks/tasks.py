"""Coding tasks with machine-checkable answers.

Each task states a problem, and `tests` is Python asserted against whatever function
the model produces. The point is a verdict that does not depend on anyone's opinion:
the code either passes or it does not.

Cases are chosen for the failure modes a smaller model actually shows - empty input,
zero, negatives, duplicates, unicode, boundary values - rather than for difficulty.
An answer that handles only the happy path fails here, which is exactly the gap the
compensation layer is meant to close.
"""

TASKS = [
    {
        "id": "median",
        "prompt": (
            "Write a Python function `median(nums)` that returns the median of a list "
            "of numbers. For an even-length list return the mean of the two middle "
            "values. Raise ValueError on an empty list. Output only the function."
        ),
        "entry": "median",
        "tests": """
assert median([1,2,3]) == 2
assert median([1,3]) == 2
assert median([3,1,2]) == 2, "must sort first"
assert median([1,2,3,4]) == 2.5
assert median([-5,-1,-3]) == -3
assert median([7]) == 7
try:
    median([]); raise AssertionError("empty list must raise ValueError")
except ValueError:
    pass
""",
    },
    {
        "id": "chunk",
        "prompt": (
            "Write a Python function `chunk(items, size)` splitting a list into "
            "consecutive sublists of length `size`, the last possibly shorter. "
            "Raise ValueError if size < 1. Output only the function."
        ),
        "entry": "chunk",
        "tests": """
assert chunk([1,2,3,4,5], 2) == [[1,2],[3,4],[5]]
assert chunk([], 3) == []
assert chunk([1,2,3], 5) == [[1,2,3]]
assert chunk([1,2,3,4], 4) == [[1,2,3,4]]
for bad in (0, -1):
    try:
        chunk([1,2], bad); raise AssertionError(f"size={bad} must raise ValueError")
    except ValueError:
        pass
""",
    },
    {
        "id": "roman",
        "prompt": (
            "Write a Python function `to_roman(n)` converting an integer 1..3999 to a "
            "Roman numeral string. Raise ValueError outside that range. "
            "Output only the function."
        ),
        "entry": "to_roman",
        "tests": """
assert to_roman(1) == "I"
assert to_roman(4) == "IV", "subtractive notation"
assert to_roman(9) == "IX"
assert to_roman(14) == "XIV"
assert to_roman(40) == "XL"
assert to_roman(90) == "XC"
assert to_roman(400) == "CD"
assert to_roman(900) == "CM"
assert to_roman(1994) == "MCMXCIV"
assert to_roman(3999) == "MMMCMXCIX"
for bad in (0, 4000, -1):
    try:
        to_roman(bad); raise AssertionError(f"{bad} must raise ValueError")
    except ValueError:
        pass
""",
    },
    {
        "id": "merge_intervals",
        "prompt": (
            "Write a Python function `merge(intervals)` taking a list of [start, end] "
            "pairs and returning them merged and sorted, overlapping or touching "
            "intervals combined. Output only the function."
        ),
        "entry": "merge",
        "tests": """
assert merge([[1,3],[2,6],[8,10]]) == [[1,6],[8,10]]
assert merge([]) == []
assert merge([[1,4],[4,5]]) == [[1,5]], "touching intervals merge"
assert merge([[5,6],[1,2]]) == [[1,2],[5,6]], "unsorted input"
assert merge([[1,10],[2,3]]) == [[1,10]], "fully contained"
assert merge([[1,2]]) == [[1,2]]
""",
    },
    {
        "id": "word_count",
        "prompt": (
            "Write a Python function `word_count(text)` returning a dict mapping each "
            "distinct word to how often it appears. Words are case-insensitive and "
            "punctuation must be stripped. Output only the function."
        ),
        "entry": "word_count",
        "tests": """
assert word_count("the cat the") == {"the": 2, "cat": 1}
assert word_count("") == {}
assert word_count("Hello, hello!") == {"hello": 2}, "case and punctuation"
assert word_count("a  b") == {"a": 1, "b": 1}, "repeated whitespace"
r = word_count("don't stop")
assert sum(r.values()) == 2, f"unexpected split: {r}"
""",
    },
    {
        "id": "flatten",
        "prompt": (
            "Write a Python function `flatten(nested)` that flattens an arbitrarily "
            "nested list into a single flat list, preserving order. Strings must be "
            "treated as values, not iterated. Output only the function."
        ),
        "entry": "flatten",
        "tests": """
assert flatten([1,[2,[3,[4]]]]) == [1,2,3,4]
assert flatten([]) == []
assert flatten([[],[[]]]) == []
assert flatten(["ab",["cd"]]) == ["ab","cd"], "strings must not be exploded"
assert flatten([1,[2,3],4]) == [1,2,3,4]
""",
    },
    {
        "id": "parse_version",
        "prompt": (
            "Write a Python function `compare_versions(a, b)` comparing dotted version "
            "strings, returning -1, 0 or 1. '1.0' and '1.0.0' are equal. Segments are "
            "compared numerically, not as strings. Output only the function."
        ),
        "entry": "compare_versions",
        "tests": """
assert compare_versions("1.0", "1.0.0") == 0, "trailing zeros are equal"
assert compare_versions("1.2", "1.10") == -1, "numeric, not lexicographic"
assert compare_versions("2.0", "1.9.9") == 1
assert compare_versions("1.0.1", "1.0") == 1
assert compare_versions("0.0.1", "0.0.1") == 0
""",
    },
    {
        "id": "retry_backoff",
        "prompt": (
            "Write a Python function `backoff_delays(attempts, base, cap)` returning a "
            "list of exponential backoff delays: base*2**i for i in range(attempts), "
            "each capped at `cap`. Return [] for attempts <= 0. Raise ValueError if "
            "base <= 0. Output only the function."
        ),
        "entry": "backoff_delays",
        "tests": """
assert backoff_delays(4, 1, 100) == [1,2,4,8]
assert backoff_delays(4, 1, 3) == [1,2,3,3], "must be capped"
assert backoff_delays(0, 1, 10) == []
assert backoff_delays(-2, 1, 10) == []
try:
    backoff_delays(3, 0, 10); raise AssertionError("base=0 must raise ValueError")
except ValueError:
    pass
""",
    },
]

# Harder cases. The easy tasks above ceiling out - both arms pass them - so they cannot
# show whether the compensation helps. These carry a specific trap that a model which
# writes the obvious solution will fall into: month-end clamping, quoted separators,
# cycle detection, floating point, eviction order. Exactly the discipline the operating
# notes are meant to instil.
HARD_TASKS = [
    {
        "id": "add_months",
        "prompt": (
            "Write a Python function `add_months(year, month, day, n)` returning a "
            "(year, month, day) tuple n months later. If the day does not exist in the "
            "target month, clamp to the last day of that month. n may be negative. "
            "Output only the function, using no imports."
        ),
        "entry": "add_months",
        "tests": """
assert add_months(2026, 1, 31, 1) == (2026, 2, 28), "Jan 31 + 1 month clamps to Feb 28"
assert add_months(2024, 1, 31, 1) == (2024, 2, 29), "leap year"
assert add_months(2026, 3, 31, -1) == (2026, 2, 28), "negative, clamped"
assert add_months(2026, 12, 15, 1) == (2027, 1, 15), "year rolls forward"
assert add_months(2026, 1, 15, -1) == (2025, 12, 15), "year rolls back"
assert add_months(2026, 5, 31, 1) == (2026, 6, 30), "31-day to 30-day month"
assert add_months(2026, 6, 10, 0) == (2026, 6, 10)
assert add_months(2026, 1, 31, 13) == (2027, 2, 28), "more than a year"
""",
    },
    {
        "id": "parse_csv_line",
        "prompt": (
            "Write a Python function `parse_csv_line(line)` splitting one CSV line into "
            "a list of fields. Fields may be double-quoted; a quoted field can contain "
            "commas, and a doubled quote (\"\") inside a quoted field means a literal "
            "quote. Do not use the csv module. Output only the function."
        ),
        "entry": "parse_csv_line",
        "tests": """
assert parse_csv_line('a,b,c') == ['a','b','c']
assert parse_csv_line('a,"b,c",d') == ['a','b,c','d'], "comma inside quotes"
assert parse_csv_line('"a""b"') == ['a"b'], "doubled quote is a literal quote"
assert parse_csv_line('') == ['']
assert parse_csv_line('a,,b') == ['a','','b'], "empty field"
assert parse_csv_line('"",x') == ['','x'], "empty quoted field"
assert parse_csv_line('a,"b"') == ['a','b']
""",
    },
    {
        "id": "toposort",
        "prompt": (
            "Write a Python function `toposort(graph)` where graph maps a node to a list "
            "of nodes it depends on. Return a list ordering every node after its "
            "dependencies. Raise ValueError if there is a cycle. Output only the function."
        ),
        "entry": "toposort",
        "tests": """
r = toposort({'a': [], 'b': ['a'], 'c': ['b']})
assert r.index('a') < r.index('b') < r.index('c')
assert toposort({}) == []
r = toposort({'a': [], 'b': []})
assert sorted(r) == ['a','b']
r = toposort({'a': ['b'], 'b': [], 'c': ['a','b']})
assert r.index('b') < r.index('a') < r.index('c')
try:
    toposort({'a': ['b'], 'b': ['a']}); raise AssertionError("cycle must raise ValueError")
except ValueError:
    pass
try:
    toposort({'a': ['a']}); raise AssertionError("self-cycle must raise ValueError")
except ValueError:
    pass
""",
    },
    {
        "id": "lru",
        "prompt": (
            "Write a Python class `LRU` with `__init__(self, capacity)`, `get(key)` "
            "returning the value or None, and `put(key, value)`. When over capacity it "
            "evicts the least recently used entry; both get and put count as use. "
            "Raise ValueError if capacity < 1. Output only the class."
        ),
        "entry": "LRU",
        "tests": """
c = LRU(2)
c.put('a',1); c.put('b',2)
assert c.get('a') == 1
c.put('c',3)                      # 'b' is least recently used
assert c.get('b') is None, "get() must count as a use"
assert c.get('c') == 3
c2 = LRU(1)
c2.put('x',1); c2.put('y',2)
assert c2.get('x') is None and c2.get('y') == 2
c3 = LRU(2)
c3.put('a',1); c3.put('a',9)
assert c3.get('a') == 9, "re-putting a key must update, not duplicate"
try:
    LRU(0); raise AssertionError("capacity 0 must raise ValueError")
except ValueError:
    pass
""",
    },
    {
        "id": "running_median",
        "prompt": (
            "Write a Python function `running_stats(nums)` returning a list of "
            "(count, mean, minimum, maximum) tuples, one per prefix of the input. "
            "The mean must be exact for floats. Return [] for empty input. "
            "Output only the function."
        ),
        "entry": "running_stats",
        "tests": """
assert running_stats([]) == []
assert running_stats([2]) == [(1, 2.0, 2, 2)]
r = running_stats([1,2,3])
assert r[0] == (1,1.0,1,1) and r[2] == (3,2.0,1,3)
r = running_stats([0.1,0.2])
assert abs(r[1][1] - 0.15000000000000002) < 1e-12 or abs(r[1][1] - 0.15) < 1e-12
r = running_stats([-5,-1])
assert r[1] == (2,-3.0,-5,-1), "negatives"
""",
    },
    {
        "id": "glob_match",
        "prompt": (
            "Write a Python function `matches(pattern, text)` implementing glob matching "
            "where * matches any sequence including empty, and ? matches exactly one "
            "character. No other metacharacters. Do not use fnmatch or re. "
            "Output only the function."
        ),
        "entry": "matches",
        "tests": """
assert matches('*', '') is True
assert matches('*', 'anything') is True
assert matches('a*c', 'abc') is True
assert matches('a*c', 'ac') is True, "* matches empty"
assert matches('a?c', 'abc') is True
assert matches('a?c', 'ac') is False, "? needs exactly one char"
assert matches('', '') is True
assert matches('', 'x') is False
assert matches('a*b*c', 'axxbyyc') is True
assert matches('*.py', 'main.py') is True
assert matches('*.py', 'main.pyc') is False
""",
    },
]

TASKS = TASKS + HARD_TASKS
