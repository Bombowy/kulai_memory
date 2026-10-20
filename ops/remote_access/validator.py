"""Read-only deployment review. Missing evidence is NOT VERIFIED, never success.

Configuration and expanded nginx -T input remain local; output contains check IDs
only. No service, network, firewall, DDL, revocation or automatic repair commands.
"""
import argparse
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
from urllib.parse import urlsplit

from ops.remote_access.package import Deployment
from ops.remote_access.journal_status import within_budget


@dataclass(frozen=True)
class Check:
    name: str
    result: str


def result(checks):
    return {'result': 'BLOCKED' if any(c.result=='FAIL' for c in checks) else
            'NOT VERIFIED' if any(c.result=='NOT VERIFIED' for c in checks) else 'READY FOR REVIEW',
            'dry_run': True, 'production_go': False,
            'checks': [dict(name=c.name,result=c.result) for c in checks]}


def nginx_directives(content):
    """Conservative config lexer, not an HTTP parser. Requires expanded inventory."""
    if len(content)>4*1024*1024: raise ValueError()
    lexer=shlex.shlex(content,posix=True,punctuation_chars='{};')
    lexer.whitespace_split=True
    lexer.commenters='#'
    root=[]
    stack=[root]
    words=[]
    for token in lexer:
        if token==';':
            if not words: raise ValueError()
            stack[-1].append((words,[])); words=[]
        elif token=='{':
            if not words: raise ValueError()
            child=[]; stack[-1].append((words,child)); stack.append(child); words=[]
        elif token=='}':
            if words or len(stack)==1: raise ValueError()
            stack.pop()
        elif token in ('};','{{','}}'):
            # Refuse unsupported ambiguous formatting rather than guess.
            raise ValueError()
        else: words.append(token)
    if words or len(stack)!=1: raise ValueError()
    return root


def walk(nodes):
    for args,children in nodes:
        yield args,children
        yield from walk(children)


def nginx_inventory(config, content):
    nodes=nginx_directives(content)
    directives=list(walk(nodes))
    host=urlsplit(config.public_origin).hostname
    port=urlsplit(config.public_origin).port or 443
    conflicts=[]
    defaults=0
    relevant=[]
    for args,children in directives:
        if args[0]=='map' and args[-1].startswith('$kulai_'): conflicts.append('namespace')
        if args[0] in ('set','geo','split_clients') and any(a.startswith('$kulai_') for a in args[1:]): conflicts.append('namespace')
        if args[0]=='log_format' and args[1]=='kulai_safe': conflicts.append('namespace')
        if args[0] in ('limit_req_zone','limit_conn_zone') and any(a.startswith('zone=kulai_') for a in args): conflicts.append('namespace')
        if args[0]!='server' or not children: continue
        listeners=[a for a,_ in children if a[0]=='listen']
        names=[v for a,_ in children if a[0]=='server_name' for v in a[1:]]
        for name in names:
            if name==host or name.startswith(('~','*')) or name.endswith('*') or name.startswith('.'):
                # Regex/wildcard may own the candidate domain: operator review required.
                conflicts.append('domain')
        for listen in listeners:
            endpoint=listen[1]
            owns=endpoint in (str(port),f'*:{port}',f'[::]:{port}',f'0.0.0.0:{port}',f'{config.public_listen_address}:{port}')
            if endpoint.endswith(':'+str(port)) and not owns:
                try: ipaddress.ip_address(endpoint.rsplit(':',1)[0])
                except ValueError: conflicts.append('ambiguous_listen_address')
            if not owns: continue
            relevant.append(listen)
            if 'ssl' not in listen: conflicts.append('non_tls_listener')
            if 'default_server' in listen: defaults+=1
            if any(x.startswith(('http2','quic','proxy_protocol','reuseport','bind')) for x in listen[2:]):
                conflicts.append('listener_options_require_review')
    if defaults>1: conflicts.append('multiple_defaults')
    if not config.shared_ip and relevant: conflicts.append('dedicated_address_owned')
    if config.shared_ip and defaults!=1:
        # Otherwise the new vhost could silently become the default. Never fix here.
        conflicts.append('existing_default_required')
    return not conflicts


def certificate(path, identity, *, purpose, ca=None, openssl=None, minimum_days=7):
    from cryptography import x509
    from cryptography.x509.oid import ExtendedKeyUsageOID
    cert=x509.load_pem_x509_certificate(Path(path).read_bytes())
    now=datetime.now(timezone.utc)
    if not cert.not_valid_before_utc<=now or cert.not_valid_after_utc<now+timedelta(days=minimum_days): return False
    names=cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    if identity not in names or any('*' in n for n in names): return False
    eku=cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    if (ExtendedKeyUsageOID.SERVER_AUTH if purpose=='ssl_server' else ExtendedKeyUsageOID.CLIENT_AUTH) not in eku: return False
    if ca is None or openssl is None: return None
    for authority in x509.load_pem_x509_certificates(Path(ca).read_bytes()):
        if not authority.not_valid_before_utc<=now or authority.not_valid_after_utc<now+timedelta(days=minimum_days): return False
    command=[str(openssl),'verify','-x509_strict','-purpose',purpose.replace('_',''),'-verify_hostname',identity,'-CAfile',str(ca),str(path)]
    return subprocess.run(command,capture_output=True,timeout=10).returncode==0


def validate(config, inventory, *, existing_nginx=None, candidates=None, certificates=None, openssl=None):
    checks=[]
    def check(name,value): checks.append(Check(name,'NOT VERIFIED' if value is None else 'PASS' if value else 'FAIL'))
    host=urlsplit(config.public_origin).hostname
    try: ipaddress.ip_address(host); domain=False
    except ValueError: domain='.' in host and host!='localhost' and not host.endswith('.localhost')
    check('explicit_https_wss_origins',domain and not config.test_loopback and not config.test_listen_port)
    try:
        if candidates: config.validate_candidates(Path(candidates))
        check('reviewed_candidate_policy',True if candidates else None)
    except Exception: check('reviewed_candidate_policy',False)
    try:
        check('existing_nginx_ownership',nginx_inventory(config,existing_nginx) if existing_nginx is not None and inventory.get('nginx_expanded') is True else None)
    except Exception: check('existing_nginx_ownership',False)
    # These are operator-collected, timestamped inventory evidence, not guessed
    # production state. The CLI never claims to have queried a remote server.
    observed=inventory.get('observed_at')
    try: fresh=timedelta(0)<=datetime.now(timezone.utc)-datetime.fromisoformat(observed)<=timedelta(hours=24)
    except Exception: fresh=None
    check('fresh_inventory',fresh)
    check('journal_capacity_and_latency_budget',within_budget(inventory.get('journal')))
    checks_needed={
        'nginx_combined_syntax':'combined_nginx_t_ok',
        'ubuntu_firewall_peer_only':'ubuntu_firewall_peer_only',
        'windows_firewall_peer_only':'windows_firewall_peer_only',
        'private_services_loopback':'private_services_loopback',
        'private_key_acl':'private_key_acl_ok',
        'rollback_reviewed':'rollback_reviewed',
        'bridge_interface_and_port':'bridge_interface_and_port_ok',
        'gateway_identity_configured':'gateway_identity_configured',
        'safe_restart_reviewed':'safe_restart_reviewed',
        'shared_default_tls_policy':'default_tls_12_13_only',
        'certificate_key_match':'certificate_key_match',
        'seal_independent_of_db_restore':'gate_outside_database_backup',
    }
    for name,key in checks_needed.items():
        value=inventory.get(key)
        check(name,value if type(value) is bool else None)
    backend=inventory.get('backend',{})
    check('secure_backend',backend['mode']=='SECURE' if backend.get('mode') is not None else None)
    check('canonical_head',backend['canonical_head'] in ('kulai_memory_0004','kulai_memory_0005') if backend.get('canonical_head') is not None else None)
    check('security_head',backend['security_head']=='kulai_security_0003' if backend.get('security_head') is not None else None)
    check('revocation_available',backend.get('revocation_available') if type(backend.get('revocation_available')) is bool else None)
    # Review is conducted with the deployment closed. Opening is a separate action.
    state=backend.get('gate_state')
    check('closed_recovery_gate',state in ('CLOSED','RECOVERED') if state is not None else None)
    check('external_seal_verified',backend.get('external_seal_verified') if type(backend.get('external_seal_verified')) is bool else None)
    tunnel=inventory.get('wireguard',{})
    expected={'ubuntu_allowed_ips':[config.windows_wg_ip+'/32'],'windows_allowed_ips':[config.vps_wg_ip+'/32'],
              'ubuntu_address':config.vps_wg_ip+'/32','windows_address':config.windows_wg_ip+'/32','persistent_keepalive':25}
    for key,value in expected.items(): check('wg_'+key,tunnel[key]==value if tunnel.get(key) is not None else None)
    routes=inventory.get('lan_vpn_routes')
    try:
        addresses=[ipaddress.ip_address(config.windows_wg_ip),ipaddress.ip_address(config.vps_wg_ip)]
        check('route_conflicts',not any(a in ipaddress.ip_network(r,strict=False) for r in routes for a in addresses) if isinstance(routes,list) else None)
    except Exception: check('route_conflicts',False)
    certs=certificates or {}
    for name,identity,purpose in (('public',urlsplit(config.public_origin).hostname,'ssl_server'),
                                  ('ingress',config.ingress_identity,'ssl_server'),
                                  ('gateway',inventory.get('expected_gateway_identity'),'ssl_client')):
        spec=certs.get(name)
        try:
            value=certificate(spec['certificate'],identity,purpose=purpose,ca=spec.get('ca'),openssl=openssl) if spec and identity else None
            check(name+'_chain_san_expiry',value)
        except Exception: check(name+'_chain_san_expiry',False)
    return result(checks)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--inventory',type=Path)
    parser.add_argument('--existing-nginx',type=Path)
    parser.add_argument('--candidates',type=Path)
    parser.add_argument('--certificates',type=Path)
    parser.add_argument('--openssl',type=Path)
    parser.add_argument('--backend-read-only',action='store_true')
    parser.add_argument('--gate',type=Path)
    args=parser.parse_args()
    try:
        load=lambda p: json.loads(p.read_text(encoding='utf-8-sig')) if p else {}
        config=Deployment(**load(args.config))
        inventory=load(args.inventory)
        if args.backend_read_only:
            import asyncio
            inventory['backend']=asyncio.run(probe_backend(args.gate))
        output=validate(config,inventory,existing_nginx=args.existing_nginx.read_text(encoding='utf-8') if args.existing_nginx else None,
                        candidates=args.candidates,certificates=load(args.certificates),openssl=args.openssl)
    except Exception: output={'result':'BLOCKED','dry_run':True,'production_go':False,'detail':'Configuration validation unavailable.'}
    print(json.dumps(output))
    return 0 if output['result']=='READY FOR REVIEW' else 2

async def probe_backend(directory):
    """Actual local read-only DB evidence. Never calls gate.check (which can close)."""
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text
    from kulai_memory.database_safety import database_config,run_database_doctor
    from kulai_memory.settings import get_settings
    from kulai_memory.security_recovery.gate import RecoveryGate,checkpoint,locked
    config=database_config()
    mode=get_settings().kulai_security_mode.value
    gate=RecoveryGate(directory)
    engine=create_async_engine(config.async_url,echo=False,hide_parameters=True,
        connect_args={'server_settings':{'statement_timeout':'5000','lock_timeout':'3000'}})
    try:
        async with locked(gate.mutex):
            state=gate.read()
            doctor=await run_database_doctor(async_url=config.async_url,require_security=True)
            async with engine.connect() as connection:
                await connection.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
                value=await checkpoint(connection)
                canonical_heads=tuple((await connection.execute(text('SELECT version_num FROM public.alembic_version ORDER BY version_num'))).scalars())
                await connection.rollback()
            return dict(mode=mode,canonical_head=canonical_heads[0],security_head='kulai_security_0003',
                        revocation_available=doctor.ok,gate_state=state['state'],external_seal_verified=gate.matches(state,value))
    finally: await engine.dispose()


if __name__=='__main__': raise SystemExit(main())
