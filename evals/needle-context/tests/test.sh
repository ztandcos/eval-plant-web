#!/bin/sh

exists=failed; value=failed
[ -f /app/token.txt ] && exists=passed
[ "$(tr -d ' \n' < /app/token.txt 2>/dev/null)" = "EVALPLANT-NEEDLE-7F3A" ] && value=passed
TESTS="{\"name\":\"token_file\",\"status\":\"$exists\"},{\"name\":\"token_value\",\"status\":\"$value\"}"
PASS=0
[ "$exists" = passed ] && [ "$value" = passed ] && PASS=1

mkdir -p /logs/verifier
cat > /logs/verifier/ctrf.json <<EOF
{"results":{"tool":{"name":"shell","version":"1"},"tests":[$TESTS]}}
EOF
if [ "$PASS" = 1 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
