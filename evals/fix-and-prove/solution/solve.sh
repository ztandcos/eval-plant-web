#!/bin/sh
cat > /app/calc.py <<'PY'
def add(a, b):
    return a + b

def mul(a, b):
    return a * b
PY
python3 /app/run_tests.py >/dev/null
