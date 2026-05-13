## Reference: a Python number/math function (MBPP-style)

```python
def is_perfect_square(n: int) -> bool:
    """Return True if n is a non-negative perfect square."""
    if n < 0:
        return False
    if n == 0:
        return True
    r = int(n ** 0.5)
    # Check r and r+1 to defend against floating-point error
    for candidate in (r, r + 1):
        if candidate * candidate == n:
            return True
    return False
```

Or a counting pattern:

```python
def count_prime_factors(n: int) -> int:
    """Count distinct prime factors of n."""
    if n <= 1:
        return 0
    count = 0
    d = 2
    while d * d <= n:
        if n % d == 0:
            count += 1
            while n % d == 0:
                n //= d
        d += 1
    if n > 1:
        count += 1
    return count
```

Pattern notes:
- Guard against negatives, zero, and one as separate cases.
- Watch for floating-point precision (use `int(...)` + verify, never `==` on floats).
- Use integer arithmetic (`//`, `%`) when working with integers.
- Loop bounds: prefer `while d * d <= n` over `while d <= sqrt(n)` to avoid float comparison.

Author your solution following the same shape. Handle edge cases (0, 1, negatives). Use integer arithmetic. No global state.
