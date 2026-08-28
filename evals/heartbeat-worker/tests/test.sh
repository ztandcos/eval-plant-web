#!/bin/sh

exists=failed; moves=failed
[ -f /tmp/heartbeat ] && exists=passed
a=$(cat /tmp/heartbeat 2>/dev/null)
sleep 3
b=$(cat /tmp/heartbeat 2>/dev/null)
[ -n "$a" ] && [ -n "$b" ] && [ "$a" != "$b" ] && moves=passed
TESTS="{\"name\":\"heartbeat_exists\",\"status\":\"$exists\"},{\"name\":\"heartbeat_updates\",\"status\":\"$moves\"}"
PASS=0
[ "$exists" = passed ] && [ "$moves" = passed ] && PASS=1

mkdir -p /logs/verifier
cat > /logs/verifier/ctrf.json <<EOF
{"results":{"tool":{"name":"shell","version":"1"},"tests":[$TESTS]}}
EOF
if [ "$PASS" = 1 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
