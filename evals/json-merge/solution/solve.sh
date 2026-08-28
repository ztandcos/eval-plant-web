#!/bin/sh
python3 - <<'PY'
import json
from pathlib import Path
a=json.loads(Path('/data/a.json').read_text())
b=json.loads(Path('/data/b.json').read_text())
Path('/app/out.json').write_text(json.dumps({**a, **b}))
PY
