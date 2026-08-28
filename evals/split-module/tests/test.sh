#!/bin/sh

mod=failed; out=failed; uses=failed
[ -f /app/util.py ] && python3 -c "import sys; sys.path.insert(0,'/app'); from util import normalize; assert normalize(' Ada ')=='ada'" && mod=passed
text=$(python3 /app/app.py 2>/dev/null)
[ "$text" = "hi:ada" ] && out=passed
python3 -c "import pathlib; t=pathlib.Path('/app/app.py').read_text(); assert 'from util import normalize' in t or 'import util' in t" && uses=passed
TESTS="{\"name\":\"util_module\",\"status\":\"$mod\"},{\"name\":\"app_output\",\"status\":\"$out\"},{\"name\":\"imports_util\",\"status\":\"$uses\"}"
PASS=0
[ "$mod" = passed ] && [ "$out" = passed ] && [ "$uses" = passed ] && PASS=1

mkdir -p /logs/verifier
cat > /logs/verifier/ctrf.json <<EOF
{"results":{"tool":{"name":"shell","version":"1"},"tests":[$TESTS]}}
EOF
if [ "$PASS" = 1 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
