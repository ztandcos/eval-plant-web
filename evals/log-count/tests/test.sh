#!/bin/sh

exists=failed; value=failed
[ -f /app/count.txt ] && exists=passed
[ "$(tr -d ' \n' < /app/count.txt 2>/dev/null)" = 7 ] && value=passed
TESTS="{\"name\":\"count_file\",\"status\":\"$exists\"},{\"name\":\"count_value\",\"status\":\"$value\"}"
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
