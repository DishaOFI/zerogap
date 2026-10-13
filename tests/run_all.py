"""Runs every parity test (Python port vs the recorded output of the original n8n nodes)."""
import sys
from tests.parity import NODES, run
ok = all([run(m) for m in NODES])
print('\nALL PARITY TESTS PASSED' if ok else '\nSOME PARITY TESTS FAILED')
sys.exit(0 if ok else 1)
