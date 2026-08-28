#!/bin/sh

add=failed; mul=failed; suite=failed
python3 -c "import sys; sys.path.insert(0,'/app'); from calc import add; assert add(2,3)==5" && add=passed
python3 -c "import sys; sys.path.insert(0,'/app'); from calc import mul; assert mul(3,4)==12" && mul=passed
python3 /app/run_tests.py >/tmp/prove-out 2>/tmp/prove-err && suite=passed
TESTS="{\"name\":\"add\",\"status\":\"$add\"},{\"name\":\"mul\",\"status\":\"$mul\"},{\"name\":\"run_tests\",\"status\":\"$suite\"}"
PASS=0
[ "$add" = passed ] && [ "$mul" = passed ] && [ "$suite" = passed ] && PASS=1

mkdir -p /logs/verifier
cat > /logs/verifier/ctrf.json <<EOF
{"results":{"tool":{"name":"shell","version":"1"},"tests":[$TESTS]}}
EOF
if [ "$PASS" = 1 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
