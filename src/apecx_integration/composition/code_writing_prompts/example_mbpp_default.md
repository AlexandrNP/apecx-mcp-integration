## Reference: a small Python function (general algorithmic pattern)

```python
def has_opposite_signs(a: int, b: int) -> bool:
    """Return True if a and b have opposite signs (one negative, one positive)."""
    return (a < 0) != (b < 0) and a != 0 and b != 0
```

Or with a small loop:

```python
def find_min_positive(nums: list[int]) -> int | None:
    """Return the smallest positive number in nums, or None if none exist."""
    best = None
    for n in nums:
        if n > 0 and (best is None or n < best):
            best = n
    return best
```

Pattern notes:
- One function. Clear name. Type hints help but are optional.
- Handle empty / edge-case inputs as the first branch.
- Return a value (don't print). Single return per branch is fine; multiple early returns are also fine.
- No prose outside the function body.

Author your solution following the same shape. One function. Handle the obvious edge cases. No module-level statements other than the function definition.
