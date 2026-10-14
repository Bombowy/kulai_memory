"""Local operator recovery. Default dry-run; never auto-starts ingress or migrates."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend/src'))
from kulai_memory.database_safety import database_config, run_database_doctor, database_snapshot_url
from kulai_memory.security_recovery import RecoveryGate
from kulai_memory.security_recovery.recovery import BackupEvidence, recover, release
from kulai_memory.security_recovery.gate import locked
from kulai_memory.backup_service import create_verified_backup
from kulai_memory.settings import get_settings


async def run(args):
    config = database_config()
    name = make_url(config.async_url).database
    if args.command == 'initialize':
        if not args.apply:
            return {'result': 'NOT VERIFIED', 'dry_run': True, 'gate_exists': args.gate.exists()}
        if args.confirm_database != name: raise ValueError()
        RecoveryGate.initialize(args.gate)
        return {'result': 'BLOCKED', 'gate': 'CLOSED'}
    if args.command == 'backup':
        if not args.apply: return {'result':'NOT VERIFIED','dry_run':True,'backup_created':False}
        if args.confirm_database!=name or not args.output or not args.manifest: raise ValueError()
        if not (await run_database_doctor(async_url=config.async_url,require_security=True)).ok: raise ValueError()
        backup=await create_verified_backup(args.output,config=config,app_env=get_settings().app_env)
        BackupEvidence.from_backup(backup).write_manifest(args.manifest)
        return {'result':'READY FOR REVIEW','backup_restore_verified':True,'database_modified':False}
    gate = RecoveryGate(args.gate)
    if not args.apply:
        doctor = await run_database_doctor(async_url=config.async_url, require_security=True)
        return {'result': 'NOT VERIFIED' if doctor.ok else 'BLOCKED',
                'dry_run': True, 'gate': gate.read()['state'], 'revocation_applied': False}
    if args.confirm_database != name: raise ValueError()
    if args.command=='close':
        async with locked(gate.ingress,exclusive=True,timeout=.2),locked(gate.mutex):
            gate.append({**gate.read(),'state':'CLOSED'})
        return {'result':'BLOCKED','gate':'CLOSED','ingress_started':False}
    if args.command == 'recover':
        data = json.loads(args.manifest.read_text(encoding='utf-8'))
        data['archive'] = Path(data['archive'])
        result = await recover(config, gate, BackupEvidence(**data))
        return {'result': 'BLOCKED', 'gate': 'RECOVERED', **result}
    await release(config, gate)
    return {'result': 'READY FOR REVIEW', 'gate': 'OPEN', 'ingress_started': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['initialize', 'backup', 'close', 'recover', 'release'])
    parser.add_argument('--gate', type=Path, required=True)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--confirm-database')
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(run(args))))
        return 0
    except Exception:
        print(json.dumps({'result': 'BLOCKED', 'detail': 'Recovery verification unavailable; no automatic release.'}))
        return 1

if __name__ == '__main__': raise SystemExit(main())
