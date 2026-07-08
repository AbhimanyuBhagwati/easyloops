# python-tests
> Writing and running dependency-free Python test files.
keywords: test, tests, assert, verify
---
Write test files that need NOTHING installed (no pytest, no unittest classes):

```python
from mymodule import thing

assert thing(2) == 4, f"got {thing(2)}"
try:
    thing(None)
    assert False, "expected TypeError"
except TypeError:
    pass
print("OK")
```

Rules:
- Import the module under test at the top; run with `python3 test_x.py`.
- One assert per behavior, with a message showing the actual value.
- Exceptions: call inside try, `assert False` after the call, except the EXACT exception type.
- End the file with `print("OK")` so success is visible.
- The test file must exit 0 on success and non-zero on any failure (bare asserts do this).
- Never test by printing and eyeballing; always assert.
- FORBIDDEN: `if ok: print("passed") else: print("failed")` — a test that PRINTS
  "failed" but exits 0 is worse than no test. Every check must be an `assert` so a
  failure makes the file exit non-zero.
- If the module reads/writes files, use tempfile.mkdtemp() paths, never real user paths.
