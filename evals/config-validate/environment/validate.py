import pathlib, sys
required = {'NAME': None, 'PORT': None, 'MODE': 'prod'}
values = {}
path = pathlib.Path('/app/service.conf')
if not path.exists():
    raise SystemExit('missing /app/service.conf')
for raw in path.read_text().splitlines():
    line = raw.strip()
    if not line or line.startswith('#'): continue
    if '=' not in line: raise SystemExit('bad line: ' + line)
    key, value = line.split('=', 1)
    values[key.strip()] = value.strip()
for key, expected in required.items():
    if key not in values: raise SystemExit('missing ' + key)
    if expected is not None and values[key] != expected:
        raise SystemExit(key + ' must be ' + expected)
if not values['PORT'].isdigit(): raise SystemExit('PORT must be int')
print('ok')
