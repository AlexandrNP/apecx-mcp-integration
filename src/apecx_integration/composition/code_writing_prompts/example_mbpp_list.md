## Reference: a Python list/sequence function (MBPP-style)

```python
def sort_by_second(pairs: list[tuple]) -> list[tuple]:
    """Sort a list of tuples by the second element of each tuple."""
    if not pairs:
        return []
    return sorted(pairs, key=lambda x: x[1])
```

Or a list-aggregation pattern:

```python
def flatten_and_sum(nested: list) -> int:
    """Flatten a list of lists and return the sum of all elements."""
    return sum(x for sub in nested for x in sub)
```

Pattern notes:
- Empty-input handling first.
- Use `sorted(..., key=...)`, list comprehensions, and `sum`/`max`/`min` rather than manual loops.
- Return a new list / value, do not mutate the input.

Author your solution following the same shape. Handle empty lists. Prefer comprehensions over imperative loops. No global state.
