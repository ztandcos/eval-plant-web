#!/bin/sh
cat > /app/sum.py <<'PY'
def sum_to(n):
    return n * (n + 1) // 2
PY
