#!/bin/sh

exists=failed
content=failed
[ -f /app/answer.txt ] && exists=passed
[ "$(cat /app/answer.txt 2>/dev/null)" = "EvalPlant works" ] && content=passed

mkdir -p /logs/verifier
cat > /logs/verifier/ctrf.json <<EOF
{"results":{"tool":{"name":"shell","version":"1"},"tests":[{"name":"answer_file_exists","status":"$exists"},{"name":"answer_file_content","status":"$content"}]}}
EOF

if [ "$exists" = passed ] && [ "$content" = passed ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
