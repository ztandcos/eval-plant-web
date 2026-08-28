import sys
sys.path.insert(0, '/app')
from calc import add, mul
assert add(2, 3) == 5, add(2, 3)
assert mul(3, 4) == 12, mul(3, 4)
print('ok')
