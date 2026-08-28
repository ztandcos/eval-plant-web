#!/bin/sh
python3 - <<'PY' &
import time
from pathlib import Path
while True:
    Path('/tmp/heartbeat').write_text(str(int(time.time())))
    time.sleep(1)
PY
sleep 1
