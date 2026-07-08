# json-storage
> Persisting app data to a JSON file safely with the stdlib.
keywords: json, storage, persist, save
---
Pattern for JSON-file persistence (no databases, no third-party packages):

```python
import json
from pathlib import Path

def load_data(path):
    p = Path(path)
    if not p.exists():
        return []          # sensible empty default, never crash on first run
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return []          # corrupt file: start fresh rather than crash

def save_data(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(p)         # atomic-ish: never leaves a half-written file
```

Rules:
- The storage path must be a parameter (argument or constructor), never hard-coded,
  so tests can point it at a temp directory.
- Load lazily, save after every mutation.
- Store plain lists/dicts of JSON-safe types (str, int, float, bool, None).
- Dates: store as ISO strings via datetime.isoformat(), parse with fromisoformat().
