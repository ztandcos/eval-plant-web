#!/bin/sh

exists=failed; valid=failed
[ -f /app/service.conf ] && exists=passed
python3 /app/validate.py >/tmp/validate-out 2>/tmp/validate-err && valid=passed
TESTS="{\"name\":\"config_exists\",\"status\":\"$exists\"},{\"name\":\"validator_ok\",\"status\":\"$valid\"}"
PASS=0
[ "$exists" = passed ] && [ "$valid" = passed ] && PASS=1

mkdir -p /logs/verifier
cat > /logs/verifier/ctrf.json <<EOF
{"results":{"tool":{"name":"shell","version":"1"},"tests":[$TESTS]}}
EOF
if [ "$PASS" = 1 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
