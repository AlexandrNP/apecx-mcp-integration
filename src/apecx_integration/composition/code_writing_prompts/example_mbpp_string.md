## Reference: a Python string-manipulation function (MBPP-style)

```python
def remove_first_last_char(s: str, c: str) -> str:
    """Remove the first and last occurrence of character c from s."""
    if not s or not c:
        return s
    first = s.find(c)
    last = s.rfind(c)
    if first == -1:
        return s
    if first == last:
        return s[:first] + s[first + 1:]
    return s[:first] + s[first + 1:last] + s[last + 1:]
```

Pattern notes:
- Handle edge cases at the top: empty input, missing target, single occurrence.
- Use Python's stdlib string methods (`find`, `rfind`, slicing) rather than reimplementing.
- Return early when no work is needed.
- Type hints are optional but help.

Author your solution following the same shape: define one function, handle empty/single-element cases first, then the general case. No prose, no comments outside the code, no module-level statements other than the function definition.
