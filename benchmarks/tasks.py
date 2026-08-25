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
