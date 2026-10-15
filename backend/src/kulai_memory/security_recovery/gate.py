"""External durable security checkpoint. Never initialized by application startup.

The append journal is protected by OS permissions, not a home-grown signature.
Its hash chain detects corruption; a trusted OS/operator must prevent journal
rollback together with a DB backup. Missing/partial/pending state fails closed.
"""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess

from sqlalchemy import text
from kulai_memory.security.models import SecurityError
from kulai_memory.security_persistence.integrity import security_snapshot

REPO = Path(__file__).resolve().parents[4]
MAX_JOURNAL = 16 * 1024 * 1024


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


class FileLease:
    """Nonblocking cross-process locks; a lease lives as long as its open handle."""
    def __init__(self, path, *, exclusive):
        self.fd = os.open(path, os.O_RDWR | getattr(os, 'O_BINARY', 0))
        self.locked = False
        self.exclusive = exclusive
        self.overlapped = None

    def try_lock(self):
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            import msvcrt
            class Overlapped(ctypes.Structure):
                _fields_ = [('Internal', ctypes.c_size_t), ('InternalHigh', ctypes.c_size_t),
                            ('Offset', wintypes.DWORD), ('OffsetHigh', wintypes.DWORD), ('hEvent', wintypes.HANDLE)]
            self.overlapped = Overlapped()
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            lock = kernel.LockFileEx
            lock.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                             wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
            lock.restype = wintypes.BOOL
            if not lock(msvcrt.get_osfhandle(self.fd), 1 | (2 if self.exclusive else 0), 0, 1, 0, ctypes.byref(self.overlapped)):
                if ctypes.get_last_error() in (33, 158): return False
                raise SecurityError()
        else:
            import fcntl
            try: fcntl.flock(self.fd, fcntl.LOCK_NB | (fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH))
            except BlockingIOError: return False
        self.locked = True
        return True

    def close(self):
        if self.fd is None: return
        try:
            if self.locked and os.name == 'nt':
                import ctypes
                from ctypes import wintypes
                import msvcrt
                unlock = ctypes.WinDLL('kernel32', use_last_error=True).UnlockFileEx
                unlock.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                   wintypes.DWORD, ctypes.c_void_p]
                unlock.restype = wintypes.BOOL
                unlock(msvcrt.get_osfhandle(self.fd), 0, 1, 0, ctypes.byref(self.overlapped))
        finally:
            os.close(self.fd)
            self.fd = None


@asynccontextmanager
async def locked(path, *, exclusive=True, timeout=5):
    lease = FileLease(path, exclusive=exclusive)
    try:
        async with asyncio.timeout(timeout):
            while not lease.try_lock(): await asyncio.sleep(.01)
        yield lease
    finally:
        lease.close()


def local_directory_path(path):
    path = Path(path)
    if os.name == 'nt':
        import ctypes
        if str(path).startswith('\\\\') or ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor)) != 3:
            raise SecurityError()  # No UNC, mapped network or ephemeral removable drive.
    if not path.is_absolute() or path.resolve() != path or path.is_relative_to(REPO):
        raise SecurityError()
    return path


def private_directory(path):
    """Read-only permission check. No chmod/icacls on production directories."""
    path = local_directory_path(path)
    if not path.is_dir() or path.is_symlink(): raise SecurityError()
    if os.name == 'nt':
        script = REPO / 'ops/remote_access/windows/check-private-directory.ps1'
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'RemoteSigned',
            '-File', str(script), '-Directory', str(path)], capture_output=True, timeout=10)
        if result.returncode != 0 or result.stdout.strip() != b'PRIVATE': raise SecurityError()
    else:
        info = path.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077: raise SecurityError()


async def checkpoint(connection):
    """Same transaction/snapshot as the security operation; no credential output."""
    # Same repeatable-read transaction, same metadata as the former three
    # SELECTs, one round trip. This is never retained between operations/frames.
    metadata = (await connection.execute(text('''
        SELECT datname, oid::text,
            ARRAY(SELECT version_num FROM public.alembic_version ORDER BY version_num)
        FROM pg_database WHERE datname=current_database()
    '''))).one()
    name, identity, heads = metadata[0], metadata[1], tuple(metadata[2])
    security = await security_snapshot(connection)
    if heads != ('kulai_memory_0004',) or security is None or security.revision != ('kulai_security_0003',):
        raise SecurityError()
    return {'database': name, 'oid': identity}, hashlib.sha256(canonical(asdict(security))).hexdigest()


class RecoveryGate:
    def __init__(self, directory):
        self.directory = Path(directory)
        private_directory(self.directory)
        self.journal = self.directory / 'recovery.jsonl'
        self.mutex = self.directory / 'checkpoint.lock'
        self.ingress = self.directory / 'ingress.lock'
        for path in (self.journal, self.mutex, self.ingress):
            value = path.stat(follow_symlinks=False)
            if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1 or path.is_symlink(): raise SecurityError()
            if os.name != 'nt' and (value.st_uid != os.getuid() or value.st_mode & 0o077): raise SecurityError()

    @classmethod
    def initialize(cls, directory):
        path = local_directory_path(directory)
        path.mkdir(mode=0o700, exist_ok=False)
        for name in ('recovery.jsonl', 'checkpoint.lock', 'ingress.lock'):
            fd = os.open(path / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                if name.endswith('.lock'): os.write(fd, b'L')
                os.fsync(fd)
            finally: os.close(fd)
        gate = cls(path)
        gate.append({'state': 'CLOSED', 'identity': None, 'security': None}, initial=True)
        return gate

    def read(self):
        try:
            with self.journal.open('rb') as source: data = source.read(MAX_JOURNAL + 1)
            if not data or len(data) > MAX_JOURNAL or not data.endswith(b'\n'): raise SecurityError()
            previous = '0' * 64
            last = None
            for sequence, line in enumerate(data.splitlines(), 1):
                record = json.loads(line)
                checksum = record.pop('checksum')
                if record['seq'] != sequence or record['previous'] != previous or hashlib.sha256(canonical(record)).hexdigest() != checksum:
                    raise SecurityError()
                if record['state'] not in ('CLOSED', 'PENDING', 'RECOVERED', 'OPEN'): raise SecurityError()
                previous, last = checksum, record
            return {**last, 'checksum': previous}
        except Exception: raise SecurityError() from None

    def append(self, state, *, initial=False):
        # Caller holds checkpoint mutex (or owns exclusive new initialization).
        before = None if initial else self.read()
        clean = {key: value for key, value in state.items() if key not in ('checksum', 'seq', 'previous')}
        record = {**clean, 'seq': 1 if before is None else before['seq'] + 1,
                  'previous': '0' * 64 if before is None else before['checksum']}
        record['checksum'] = hashlib.sha256(canonical(record)).hexdigest()
        payload = canonical(record) + b'\n'
        if self.journal.stat().st_size + len(payload) > MAX_JOURNAL: raise SecurityError()
        with self.journal.open('ab', buffering=0) as target:
            if target.write(payload) != len(payload): raise SecurityError()
            os.fsync(target.fileno())  # Acknowledged durable CLOSED precedes SQL COMMIT.

    def matches(self, record, value):
        return record.get('identity') == value[0] and record.get('security') == value[1]

    async def check(self, factory):
        async with locked(self.mutex):
            state = self.read()
            if state['state'] != 'OPEN': raise SecurityError()
            async with factory() as session:
                async with session.begin():
                    await session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
                    value = await checkpoint(await session.connection())
            if not self.matches(state, value):
                self.append({**state, 'state': 'CLOSED'})
                raise SecurityError()

    @asynccontextmanager
    async def transaction(self, factory, *, readonly=False):
        # Shared lease forbids administrative recovery while any operation runs.
        async with locked(self.ingress, exclusive=False), locked(self.mutex):
            before = self.read()
            if before['state'] != 'OPEN': raise SecurityError()
            async with factory() as session:
                await session.begin()
                await session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ' + (', READ ONLY' if readonly else '')))
                value = await checkpoint(await session.connection())
                if not self.matches(before, value):
                    self.append({**before, 'state': 'CLOSED'})
                    raise SecurityError()
                pending = not readonly
                commit_attempted = False
                if pending: self.append({**before, 'state': 'PENDING'})
                try:
                    yield session
                    if readonly:
                        await session.rollback()
                    else:
                        value = await checkpoint(await session.connection())
                        commit_attempted = True
                        await session.commit()
                        self.append({**before, 'state': 'OPEN', 'identity': value[0], 'security': value[1]})
                except BaseException:
                    # A known, completed rollback before any COMMIT may restore
                    # the predecessor checkpoint. Ambiguous COMMIT never does.
                    if pending and not commit_attempted:
                        try:
                            await session.rollback()
                            self.append({**before, 'state': 'OPEN'})
                        except BaseException: pass
                    raise
