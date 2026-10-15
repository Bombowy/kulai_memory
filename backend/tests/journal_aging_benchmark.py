"""Isolated journal fixtures and full-chain timing; never operates on a live gate."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from backend.tests.security_gate_benchmark import distribution, memory
from kulai_memory.security_recovery.gate import RecoveryGate, MAX_JOURNAL, canonical
from kulai_memory.security import SecurityError


def fill_fixture(gate, state, *, records=None):
    """Generate valid synthetic history in linear time, not an admin write API.

    Only newly created, isolated test directories may be passed. All records are
    subsequently verified by the unchanged production full-chain reader.
    """
    assert gate.directory.parent.name.startswith(('kulai-owned-', 'test_'))
    previous = '0'*64
    count = size = 0
    with gate.journal.open('wb') as stream:
        while records is None or count < records:
            record = {**state, 'seq': count+1, 'previous': previous}
            record['checksum'] = hashlib.sha256(canonical(record)).hexdigest()
            payload = canonical(record)+b'\n'
            if size+len(payload) > MAX_JOURNAL: break
            stream.write(payload)
            size += len(payload)
            count += 1
            previous = record['checksum']
        stream.flush()
        os.fsync(stream.fileno())
    return count, size


def run(output):
    if not output.is_absolute() or output.exists(): raise ValueError('New absolute output required')
    rows = []
    with tempfile.TemporaryDirectory(prefix='kulai-owned-aging-') as directory:
        for number in (2, 1000, None):
            gate = RecoveryGate.initialize(Path(directory)/str(number))
            # Synthetic only, same fixed-width fingerprint/identity as a live record.
            state = dict(state='OPEN', identity={'database':'kulai_memory_backup_test_synthetic', 'oid':'123456'}, security='a'*64)
            count, size = fill_fixture(gate, state, records=number)
            times = []; cpu = time.process_time(); rss = memory()
            for _ in range(5):
                start = time.perf_counter()
                assert gate.read()['seq'] == count
                times.append((time.perf_counter()-start)*1000)
            row = dict(records=count, bytes=size, limit=MAX_JOURNAL, reads=distribution(times),
                       cpu_seconds=round(time.process_time()-cpu, 4), memory_before=rss, memory_after=memory())
            if number is None:
                before = gate.journal.read_bytes()
                try: gate.append(state)
                except SecurityError: pass
                else: raise AssertionError('Full journal accepted append')
                assert gate.journal.read_bytes() == before
                row['refused_append_without_modification'] = True
            rows.append(row)
    with output.open('x', encoding='utf-8') as stream:
        json.dump(dict(result='PASS', full_chain_verified_every_read=True, cases=rows,
                       scope='Synthetic isolated files, production 16 MiB limit, no compaction'), stream, indent=2)
    print(json.dumps(rows), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    run(parser.parse_args().output)
