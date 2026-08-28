#!/bin/sh

s1=$(python3 -c "import sys; sys.path.insert(0,'/app'); from sum import sum_to; print(sum_to(1))" 2>/dev/null)
s10=$(python3 -c "import sys; sys.path.insert(0,'/app'); from sum import sum_to; print(sum_to(10))" 2>/dev/null)
t1=failed; t10=failed
[ "$s1" = 1 ] && t1=passed
[ "$s10" = 55 ] && t10=passed
TESTS="{\"name\":\"sum_to_1\",\"status\":\"$t1\"},{\"name\":\"sum_to_10\",\"status\":\"$t10\"}"
PASS=0
[ "$t1" = passed ] && [ "$t10" = passed ] && PASS=1

mkdir -p /logs/verifier
cat > /logs/verifier/ctrf.json <<EOF
{"results":{"tool":{"name":"shell","version":"1"},"tests":[$TESTS]}}
EOF
if [ "$PASS" = 1 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
