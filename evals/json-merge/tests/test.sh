#!/bin/sh

exists=failed; keys=failed
[ -f /app/out.json ] && exists=passed
python3 - <<'PY' && keys=passed
import json
from pathlib import Path
data=json.loads(Path('/app/out.json').read_text())
assert data.get('alpha')==1 and data.get('beta')==2 and data.get('keep')=='right'
PY
TESTS="{\"name\":\"out_exists\",\"status\":\"$exists\"},{\"name\":\"merged_keys\",\"status\":\"$keys\"}"
PASS=0
[ "$exists" = passed ] && [ "$keys" = passed ] && PASS=1

mkdir -p /logs/verifier
cat > /logs/verifier/ctrf.json <<EOF
{"results":{"tool":{"name":"shell","version":"1"},"tests":[$TESTS]}}
EOF
if [ "$PASS" = 1 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
