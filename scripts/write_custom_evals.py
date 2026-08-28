#!/usr/bin/env python3
"""Generate the deterministic EvalPlant custom task set."""

from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent / "evals"

DOCKERFILE = """\
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \\
    && apt-get install -y --no-install-recommends \\
        python3 curl bash ca-certificates coreutils \\
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
"""

TASK_TOML = """\
schema_version = "1.4"

[task]
name = "evalplant/{name}"
version = "1.0.0"
description = "{description}"
authors = []
keywords = ["evalplant"]

[metadata]
difficulty = "{difficulty}"
category = "programming"
tags = {tags}

[verifier]
timeout_sec = {verifier_timeout}

[agent]
timeout_sec = {agent_timeout}

[environment]
build_timeout_sec = 180.0
cpus = 1
memory_mb = 1024
storage_mb = 2048
gpus = 0
mcp_servers = []

[verifier.env]

[solution.env]
"""


def ctrf_and_reward(checks: str) -> str:
    return f"""#!/bin/sh
{checks}
mkdir -p /logs/verifier
cat > /logs/verifier/ctrf.json <<EOF
{{"results":{{"tool":{{"name":"shell","version":"1"}},"tests":[$TESTS]}}}}
EOF
if [ "$PASS" = 1 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
"""


def write(path: Path, content: str, mode: Optional[int] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")
    if mode is not None:
        path.chmod(mode)


def task_toml(name, description, difficulty, tags, agent_timeout=90.0, verifier_timeout=30.0):
    return TASK_TOML.format(
        name=name,
        description=description,
        difficulty=difficulty,
        tags=str(tags).replace("'", '"'),
        agent_timeout=agent_timeout,
        verifier_timeout=verifier_timeout,
    )


def emit_base(name, **kwargs):
    root = ROOT / name
    write(root / "task.toml", task_toml(name, **kwargs))
    write(root / "environment" / "Dockerfile", DOCKERFILE)


def main() -> None:
    # 1. smoke-file already exists; keep it.

    emit_base(
        "fix-off-by-one",
        description="Fix an off-by-one bug in a Python sum helper",
        difficulty="easy",
        tags=["code-modification"],
    )
    write(
        ROOT / "fix-off-by-one" / "environment" / "sum.py",
        "def sum_to(n):\n    total = 0\n    for i in range(n):\n        total += i\n    return total\n",
    )
    write(
        ROOT / "fix-off-by-one" / "environment" / "Dockerfile",
        DOCKERFILE + "COPY sum.py /app/sum.py\n",
    )
    write(
        ROOT / "fix-off-by-one" / "instruction.md",
        "The file `/app/sum.py` defines `sum_to(n)`, which should return the sum of "
        "integers from 1 through n inclusive. It currently has an off-by-one bug. "
        "Fix the function in place. Do not add extra files.\n",
    )
    write(
        ROOT / "fix-off-by-one" / "solution" / "solve.sh",
        "#!/bin/sh\ncat > /app/sum.py <<'PY'\ndef sum_to(n):\n    return n * (n + 1) // 2\nPY\n",
        0o755,
    )
    write(
        ROOT / "fix-off-by-one" / "tests" / "test.sh",
        ctrf_and_reward(
            """
s1=$(python3 -c "import sys; sys.path.insert(0,'/app'); from sum import sum_to; print(sum_to(1))" 2>/dev/null)
s10=$(python3 -c "import sys; sys.path.insert(0,'/app'); from sum import sum_to; print(sum_to(10))" 2>/dev/null)
t1=failed; t10=failed
[ "$s1" = 1 ] && t1=passed
[ "$s10" = 55 ] && t10=passed
TESTS="{\\"name\\":\\"sum_to_1\\",\\"status\\":\\"$t1\\"},{\\"name\\":\\"sum_to_10\\",\\"status\\":\\"$t10\\"}"
PASS=0
[ "$t1" = passed ] && [ "$t10" = passed ] && PASS=1
"""
        ),
        0o755,
    )

    emit_base(
        "split-module",
        description="Extract a helper into util.py and import it",
        difficulty="medium",
        tags=["code-modification"],
    )
    write(
        ROOT / "split-module" / "environment" / "app.py",
        "def greet(name):\n    cleaned = str(name).strip().lower()\n    return 'hi:' + cleaned\n\n"
        "if __name__ == '__main__':\n    print(greet(' Ada '))\n",
    )
    write(
        ROOT / "split-module" / "environment" / "Dockerfile",
        DOCKERFILE + "COPY app.py /app/app.py\n",
    )
    write(
        ROOT / "split-module" / "instruction.md",
        "Refactor `/app/app.py`. Create `/app/util.py` with a function `normalize(name)` "
        "that strips whitespace and lowercases the string. `app.py` must import and use "
        "`normalize` from `util`. `python3 /app/app.py` must still print `hi:ada`.\n",
    )
    write(
        ROOT / "split-module" / "solution" / "solve.sh",
        "#!/bin/sh\ncat > /app/util.py <<'PY'\ndef normalize(name):\n    return str(name).strip().lower()\nPY\n"
        "cat > /app/app.py <<'PY'\nfrom util import normalize\n\ndef greet(name):\n    return 'hi:' + normalize(name)\n\n"
        "if __name__ == '__main__':\n    print(greet(' Ada '))\nPY\n",
        0o755,
    )
    write(
        ROOT / "split-module" / "tests" / "test.sh",
        ctrf_and_reward(
            """
mod=failed; out=failed; uses=failed
[ -f /app/util.py ] && python3 -c "import sys; sys.path.insert(0,'/app'); from util import normalize; assert normalize(' Ada ')=='ada'" && mod=passed
text=$(python3 /app/app.py 2>/dev/null)
[ "$text" = "hi:ada" ] && out=passed
python3 -c "import pathlib; t=pathlib.Path('/app/app.py').read_text(); assert 'from util import normalize' in t or 'import util' in t" && uses=passed
TESTS="{\\"name\\":\\"util_module\\",\\"status\\":\\"$mod\\"},{\\"name\\":\\"app_output\\",\\"status\\":\\"$out\\"},{\\"name\\":\\"imports_util\\",\\"status\\":\\"$uses\\"}"
PASS=0
[ "$mod" = passed ] && [ "$out" = passed ] && [ "$uses" = passed ] && PASS=1
"""
        ),
        0o755,
    )

    emit_base(
        "json-merge",
        description="Merge two JSON objects into one output file",
        difficulty="easy",
        tags=["tool-use"],
    )
    write(ROOT / "json-merge" / "environment" / "a.json", '{"alpha": 1, "keep": "left"}\n')
    write(ROOT / "json-merge" / "environment" / "b.json", '{"beta": 2, "keep": "right"}\n')
    write(
        ROOT / "json-merge" / "environment" / "Dockerfile",
        DOCKERFILE + "RUN mkdir -p /data\nCOPY a.json /data/a.json\nCOPY b.json /data/b.json\n",
    )
    write(
        ROOT / "json-merge" / "instruction.md",
        "Merge `/data/a.json` and `/data/b.json` into `/app/out.json`. "
        "Keys from b overwrite keys from a. The result must be a JSON object with "
        "`alpha=1`, `beta=2`, and `keep=\"right\"`.\n",
    )
    write(
        ROOT / "json-merge" / "solution" / "solve.sh",
        "#!/bin/sh\npython3 - <<'PY'\nimport json\nfrom pathlib import Path\n"
        "a=json.loads(Path('/data/a.json').read_text())\n"
        "b=json.loads(Path('/data/b.json').read_text())\n"
        "Path('/app/out.json').write_text(json.dumps({**a, **b}))\nPY\n",
        0o755,
    )
    write(
        ROOT / "json-merge" / "tests" / "test.sh",
        ctrf_and_reward(
            """
exists=failed; keys=failed
[ -f /app/out.json ] && exists=passed
python3 - <<'PY' && keys=passed
import json
from pathlib import Path
data=json.loads(Path('/app/out.json').read_text())
assert data.get('alpha')==1 and data.get('beta')==2 and data.get('keep')=='right'
PY
TESTS="{\\"name\\":\\"out_exists\\",\\"status\\":\\"$exists\\"},{\\"name\\":\\"merged_keys\\",\\"status\\":\\"$keys\\"}"
PASS=0
[ "$exists" = passed ] && [ "$keys" = passed ] && PASS=1
"""
        ),
        0o755,
    )

    emit_base(
        "log-count",
        description="Count matching ERROR lines with grep-style tools",
        difficulty="easy",
        tags=["tool-use"],
    )
    log_lines = []
    for i in range(1, 41):
        if i in {3, 9, 12, 21, 22, 30, 38}:
            log_lines.append(f"2026-08-27 T{i:02d}:00 ERROR payment failed id={i}")
        elif i % 5 == 0:
            log_lines.append(f"2026-08-27 T{i:02d}:00 WARN retry id={i}")
        else:
            log_lines.append(f"2026-08-27 T{i:02d}:00 INFO ok id={i}")
    write(ROOT / "log-count" / "environment" / "app.log", "\n".join(log_lines) + "\n")
    write(
        ROOT / "log-count" / "environment" / "Dockerfile",
        DOCKERFILE + "RUN mkdir -p /var/log/app\nCOPY app.log /var/log/app/app.log\n",
    )
    write(
        ROOT / "log-count" / "instruction.md",
        "Count how many lines in `/var/log/app/app.log` contain the exact token `ERROR`. "
        "Write only that integer to `/app/count.txt` with no extra whitespace besides a newline.\n",
    )
    write(
        ROOT / "log-count" / "solution" / "solve.sh",
        "#!/bin/sh\ngrep -c ERROR /var/log/app/app.log > /app/count.txt\n",
        0o755,
    )
    write(
        ROOT / "log-count" / "tests" / "test.sh",
        ctrf_and_reward(
            """
exists=failed; value=failed
[ -f /app/count.txt ] && exists=passed
[ "$(tr -d ' \\n' < /app/count.txt 2>/dev/null)" = 7 ] && value=passed
TESTS="{\\"name\\":\\"count_file\\",\\"status\\":\\"$exists\\"},{\\"name\\":\\"count_value\\",\\"status\\":\\"$value\\"}"
PASS=0
[ "$exists" = passed ] && [ "$value" = passed ] && PASS=1
"""
        ),
        0o755,
    )

    emit_base(
        "http-echo",
        description="Start an HTTP health server and keep it running",
        difficulty="medium",
        tags=["service-lifecycle"],
        agent_timeout=120.0,
        verifier_timeout=45.0,
    )
    write(
        ROOT / "http-echo" / "instruction.md",
        "Start an HTTP server listening on `127.0.0.1:8080`. A GET request to "
        "`/health` must return HTTP 200 with body exactly `ok` (optional trailing newline). "
        "The server must still be running when the verifier starts. Do not shut it down.\n",
    )
    write(
        ROOT / "http-echo" / "solution" / "solve.sh",
        "#!/bin/sh\npython3 - <<'PY' &\nfrom http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "class H(BaseHTTPRequestHandler):\n    def do_GET(self):\n"
        "        body = b'ok'\\n"
        "        self.send_response(200)\n        self.send_header('Content-Length', str(len(body)))\n"
        "        self.end_headers()\n        self.wfile.write(body)\n"
        "    def log_message(self, *args):\n        return\n"
        "HTTPServer(('127.0.0.1', 8080), H).serve_forever()\nPY\nsleep 1\n",
        0o755,
    )
    write(
        ROOT / "http-echo" / "tests" / "test.sh",
        ctrf_and_reward(
            """
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
TESTS="{\\"name\\":\\"health_status\\",\\"status\\":\\"$listen\\"},{\\"name\\":\\"health_body\\",\\"status\\":\\"$body\\"}"
PASS=0
[ "$listen" = passed ] && [ "$body" = passed ] && PASS=1
"""
        ),
        0o755,
    )

    emit_base(
        "heartbeat-worker",
        description="Keep a background worker updating a heartbeat file",
        difficulty="medium",
        tags=["service-lifecycle"],
        agent_timeout=120.0,
        verifier_timeout=45.0,
    )
    write(
        ROOT / "heartbeat-worker" / "instruction.md",
        "Start a background process that rewrites `/tmp/heartbeat` with the current Unix "
        "timestamp at least once every 2 seconds. The process must still be alive when "
        "verification begins. Do not use cron.\n",
    )
    write(
        ROOT / "heartbeat-worker" / "solution" / "solve.sh",
        "#!/bin/sh\npython3 - <<'PY' &\nimport time\nfrom pathlib import Path\nwhile True:\n"
        "    Path('/tmp/heartbeat').write_text(str(int(time.time())))\n    time.sleep(1)\nPY\nsleep 1\n",
        0o755,
    )
    write(
        ROOT / "heartbeat-worker" / "tests" / "test.sh",
        ctrf_and_reward(
            """
exists=failed; moves=failed
[ -f /tmp/heartbeat ] && exists=passed
a=$(cat /tmp/heartbeat 2>/dev/null)
sleep 3
b=$(cat /tmp/heartbeat 2>/dev/null)
[ -n "$a" ] && [ -n "$b" ] && [ "$a" != "$b" ] && moves=passed
TESTS="{\\"name\\":\\"heartbeat_exists\\",\\"status\\":\\"$exists\\"},{\\"name\\":\\"heartbeat_updates\\",\\"status\\":\\"$moves\\"}"
PASS=0
[ "$exists" = passed ] && [ "$moves" = passed ] && PASS=1
"""
        ),
        0o755,
    )

    filler = " ".join(
        f"Background note {i}: the warehouse inventory system stores pallets in aisle "
        f"{i % 17} and ignores this sentence for the actual answer."
        for i in range(1, 180)
    )
    emit_base(
        "needle-context",
        description="Recover a hidden token from a long instruction",
        difficulty="medium",
        tags=["long-context"],
        agent_timeout=180.0,
    )
    write(
        ROOT / "needle-context" / "instruction.md",
        "You will be given a long operations memo. Somewhere inside it is a unique token "
        "of the form `EVALPLANT-NEEDLE-XXXX`. Write that exact token as the only contents "
        f"of `/app/token.txt`.\n\n{filler}\n\n"
        "Maintenance window: EVALPLANT-NEEDLE-7F3A is the recovery token for rack 12.\n\n"
        f"{filler}\n",
    )
    write(
        ROOT / "needle-context" / "solution" / "solve.sh",
        "#!/bin/sh\nprintf 'EVALPLANT-NEEDLE-7F3A\\n' > /app/token.txt\n",
        0o755,
    )
    write(
        ROOT / "needle-context" / "tests" / "test.sh",
        ctrf_and_reward(
            """
exists=failed; value=failed
[ -f /app/token.txt ] && exists=passed
[ "$(tr -d ' \\n' < /app/token.txt 2>/dev/null)" = "EVALPLANT-NEEDLE-7F3A" ] && value=passed
TESTS="{\\"name\\":\\"token_file\\",\\"status\\":\\"$exists\\"},{\\"name\\":\\"token_value\\",\\"status\\":\\"$value\\"}"
PASS=0
[ "$exists" = passed ] && [ "$value" = passed ] && PASS=1
"""
        ),
        0o755,
    )

    emit_base(
        "config-validate",
        description="Write a config that passes the bundled validator before finishing",
        difficulty="medium",
        tags=["pre-completion-verification"],
        agent_timeout=120.0,
    )
    write(
        ROOT / "config-validate" / "environment" / "validate.py",
        "import pathlib, sys\nrequired = {'NAME': None, 'PORT': None, 'MODE': 'prod'}\n"
        "values = {}\npath = pathlib.Path('/app/service.conf')\n"
        "if not path.exists():\n    raise SystemExit('missing /app/service.conf')\n"
        "for raw in path.read_text().splitlines():\n"
        "    line = raw.strip()\n    if not line or line.startswith('#'): continue\n"
        "    if '=' not in line: raise SystemExit('bad line: ' + line)\n"
        "    key, value = line.split('=', 1)\n    values[key.strip()] = value.strip()\n"
        "for key, expected in required.items():\n"
        "    if key not in values: raise SystemExit('missing ' + key)\n"
        "    if expected is not None and values[key] != expected:\n"
        "        raise SystemExit(key + ' must be ' + expected)\n"
        "if not values['PORT'].isdigit(): raise SystemExit('PORT must be int')\n"
        "print('ok')\n",
    )
    write(
        ROOT / "config-validate" / "environment" / "Dockerfile",
        DOCKERFILE + "COPY validate.py /app/validate.py\n",
    )
    write(
        ROOT / "config-validate" / "instruction.md",
        "Create `/app/service.conf` as KEY=VALUE lines. Required keys: `NAME` (any non-empty "
        "string), `PORT` (integer), and `MODE` which must equal `prod`. A checker is already "
        "at `/app/validate.py`. You should run `python3 /app/validate.py` yourself and only "
        "finish after it prints `ok`.\n",
    )
    write(
        ROOT / "config-validate" / "solution" / "solve.sh",
        "#!/bin/sh\ncat > /app/service.conf <<'EOF'\nNAME=evalplant\nPORT=8080\nMODE=prod\nEOF\n"
        "python3 /app/validate.py >/dev/null\n",
        0o755,
    )
    write(
        ROOT / "config-validate" / "tests" / "test.sh",
        ctrf_and_reward(
            """
exists=failed; valid=failed
[ -f /app/service.conf ] && exists=passed
python3 /app/validate.py >/tmp/validate-out 2>/tmp/validate-err && valid=passed
TESTS="{\\"name\\":\\"config_exists\\",\\"status\\":\\"$exists\\"},{\\"name\\":\\"validator_ok\\",\\"status\\":\\"$valid\\"}"
PASS=0
[ "$exists" = passed ] && [ "$valid" = passed ] && PASS=1
"""
        ),
        0o755,
    )

    emit_base(
        "fix-and-prove",
        description="Fix a calculator and prove it by running the local tests",
        difficulty="medium",
        tags=["code-modification", "pre-completion-verification"],
        agent_timeout=120.0,
    )
    write(
        ROOT / "fix-and-prove" / "environment" / "calc.py",
        "def add(a, b):\n    return a - b\n\ndef mul(a, b):\n    return a + b\n",
    )
    write(
        ROOT / "fix-and-prove" / "environment" / "run_tests.py",
        "import sys\nsys.path.insert(0, '/app')\nfrom calc import add, mul\n"
        "assert add(2, 3) == 5, add(2, 3)\nassert mul(3, 4) == 12, mul(3, 4)\nprint('ok')\n",
    )
    write(
        ROOT / "fix-and-prove" / "environment" / "Dockerfile",
        DOCKERFILE + "COPY calc.py /app/calc.py\nCOPY run_tests.py /app/run_tests.py\n",
    )
    write(
        ROOT / "fix-and-prove" / "instruction.md",
        "`/app/calc.py` is wrong: `add` should add and `mul` should multiply. "
        "Fix the functions. Before you stop, run `python3 /app/run_tests.py` and make "
        "sure it prints `ok`. The verifier will run the same tests.\n",
    )
    write(
        ROOT / "fix-and-prove" / "solution" / "solve.sh",
        "#!/bin/sh\ncat > /app/calc.py <<'PY'\ndef add(a, b):\n    return a + b\n\ndef mul(a, b):\n    return a * b\nPY\n"
        "python3 /app/run_tests.py >/dev/null\n",
        0o755,
    )
    write(
        ROOT / "fix-and-prove" / "tests" / "test.sh",
        ctrf_and_reward(
            """
add=failed; mul=failed; suite=failed
python3 -c "import sys; sys.path.insert(0,'/app'); from calc import add; assert add(2,3)==5" && add=passed
python3 -c "import sys; sys.path.insert(0,'/app'); from calc import mul; assert mul(3,4)==12" && mul=passed
python3 /app/run_tests.py >/tmp/prove-out 2>/tmp/prove-err && suite=passed
TESTS="{\\"name\\":\\"add\\",\\"status\\":\\"$add\\"},{\\"name\\":\\"mul\\",\\"status\\":\\"$mul\\"},{\\"name\\":\\"run_tests\\",\\"status\\":\\"$suite\\"}"
PASS=0
[ "$add" = passed ] && [ "$mul" = passed ] && [ "$suite" = passed ] && PASS=1
"""
        ),
        0o755,
    )

    print("wrote custom tasks under", ROOT)


if __name__ == "__main__":
    main()
