#!/bin/sh
cat > /app/util.py <<'PY'
def normalize(name):
    return str(name).strip().lower()
PY
cat > /app/app.py <<'PY'
from util import normalize

def greet(name):
    return 'hi:' + normalize(name)

if __name__ == '__main__':
    print(greet(' Ada '))
PY
