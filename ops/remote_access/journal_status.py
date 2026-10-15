"""Read-only full-chain measurement, never initializes, closes or releases a gate.

The review budget is deliberately below exhaustion. This is operator monitoring,
not an authorization cache or a replacement for the live DB checkpoint check.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT/'backend/src') not in sys.path: sys.path.insert(0, str(ROOT/'backend/src'))

from kulai_memory.security_recovery.gate import RecoveryGate, locked

MAX_REVIEW_RECORDS = 1000
MAX_REVIEW_BYTES = 4*1024*1024
MAX_REVIEW_READ_MS = 50


def within_budget(value):
    if not isinstance(value, dict): return None
    for key in ('records', 'bytes'):
        if type(value.get(key)) is not int: return None
    if type(value.get('read_ms')) not in (int, float): return None
    return (1 <= value['records'] <= MAX_REVIEW_RECORDS and
            1 <= value['bytes'] <= MAX_REVIEW_BYTES and
            0 <= value['read_ms'] < MAX_REVIEW_READ_MS)


async def inspect(directory):
    gate = RecoveryGate(directory)
    async with locked(gate.mutex):
        start = time.perf_counter()
        state = gate.read()  # Every record verified; never gate.check/write.
        journal = dict(records=state['seq'], bytes=gate.journal.stat().st_size,
                       read_ms=round((time.perf_counter()-start)*1000, 3))
    return dict(result='NOT VERIFIED' if within_budget(journal) else 'BLOCKED',
                dry_run=True, journal=journal, gate_state=state['state'],
                review_budget_ok=within_budget(journal), checkpoint_matches_database='NOT VERIFIED',
                production_go=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gate', type=Path, required=True)
    args = parser.parse_args()
    try: result = asyncio.run(inspect(args.gate))
    except Exception:
        result = dict(result='BLOCKED', dry_run=True, production_go=False,
                      detail='Journal verification unavailable; no automatic repair or release.')
    print(json.dumps(result))
    return 2 if result['result'] == 'BLOCKED' else 0


if __name__ == '__main__': raise SystemExit(main())
