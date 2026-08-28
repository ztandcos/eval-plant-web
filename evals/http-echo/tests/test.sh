#!/bin/sh

listen=failed; body=failed
python3 - <<'PY'
import urllib.request, pathlib
status='failed'
body='failed'
try:
    with urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3) as resp:
        data=resp.read().decode().strip()
        if resp.status==200:
            status='passed'
        if data=='ok':
            body='passed'
except Exception:
    pass
pathlib.Path('/tmp/http-status').write_text(status)
pathlib.Path('/tmp/http-body').write_text(body)
PY
listen=$(cat /tmp/http-status)
body=$(cat /tmp/http-body)
TESTS="{\"name\":\"health_status\",\"status\":\"$listen\"},{\"name\":\"health_body\",\"status\":\"$body\"}"
PASS=0
[ "$listen" = passed ] && [ "$body" = passed ] && PASS=1

mkdir -p /logs/verifier
cat > /logs/verifier/ctrf.json <<EOF
{"results":{"tool":{"name":"shell","version":"1"},"tests":[$TESTS]}}
EOF
if [ "$PASS" = 1 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
