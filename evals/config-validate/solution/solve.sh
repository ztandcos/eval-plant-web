#!/bin/sh
cat > /app/service.conf <<'EOF'
NAME=evalplant
PORT=8080
MODE=prod
EOF
python3 /app/validate.py >/dev/null
