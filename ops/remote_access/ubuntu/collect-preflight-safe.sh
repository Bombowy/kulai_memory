#!/bin/sh
# Operator-run read-only handoff. No sudo, installs, SSH, network discovery,
# nginx -T, reload, firewall writes, private keys or application log reads.
set -eu
command -v python3 >/dev/null 2>&1 || { printf '%s\n' 'NOT VERIFIED: python3 unavailable; nothing changed'; exit 2; }
exec python3 - "$@" <<'PY'
import argparse
import glob
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import shlex
import subprocess
import sys
import time

DOMAIN = re.compile(r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', re.I)


def command(args):
    if not shutil.which(args[0]): return None
    try:
        result = subprocess.run(args, capture_output=True, timeout=8, env={**os.environ, 'LC_ALL':'C'})
        # Never forward stderr (it can contain private file paths/config values).
        if result.returncode or len(result.stdout) > 2*1024*1024: return None
        return result.stdout.decode('utf-8', errors='replace')
    except (OSError, subprocess.TimeoutExpired): return None


def version(args):
    value = command(args)
    if value is None: return 'NOT VERIFIED'
    found = re.search(r'\b\d+\.\d+(?:\.\d+)?[a-z]?\b', value)
    return found.group() if found else 'NOT VERIFIED'


def directives(content):
    """Only safe summaries. Partial scan NEVER validates expanded nginx config."""
    result = dict(listen=[], server_name=[], defaults=0, kulai_namespace=False)
    lexer = shlex.shlex(content, posix=True, punctuation_chars='{};')
    lexer.whitespace_split = True
    lexer.commenters = '#'
    words = []
    try:
        for token in lexer:
            if token in (';', '{'):
                if words and words[0] == 'listen':
                    result['defaults'] += int('default_server' in words[1:])
                    result['listen'].append([w for w in words[1:] if re.fullmatch(r'[0-9a-fA-F:.\[\]]+|ssl|http2|quic|default_server', w)])
                elif words and words[0] == 'server_name':
                    result['server_name'].extend(w for w in words[1:] if DOMAIN.fullmatch(w))
                if words and words[0] in ('map','geo','set','split_clients','log_format','limit_req_zone','limit_conn_zone'):
                    result['kulai_namespace'] |= any(w.startswith(('$kulai_','kulai_','zone=kulai_')) for w in words[1:])
                words = []
            elif token == '}': words = []
            elif token in ('};','}}','{{'): raise ValueError()
            else: words.append(token)
    except ValueError:
        result['partial_parse'] = 'NOT VERIFIED'
    return result


def nginx_summary():
    rows = []
    paths = ['/etc/nginx/nginx.conf']+glob.glob('/etc/nginx/sites-enabled/*')+glob.glob('/etc/nginx/conf.d/*.conf')
    for name in sorted(set(paths))[:256]:
        path = Path(name)
        try:
            resolved = path.resolve(strict=True)
            # Refuse links outside nginx or key/certificate/env filenames.
            if not resolved.is_relative_to('/etc/nginx') or resolved.suffix.lower() in ('.pem','.key','.crt','.env'): continue
            if not resolved.is_file() or resolved.stat().st_size > 1024*1024: continue
            content = resolved.read_text(encoding='utf-8')
            rows.append(dict(source_id=hashlib.sha256(name.encode()).hexdigest()[:12], **directives(content)))
        except (OSError, UnicodeError): pass
    return dict(partial_files=rows, expanded_config='NOT VERIFIED',
                reason='Only selected directive summaries. Includes, regex names, TLS policy and effective default ownership need local administrator review.')


def addresses():
    raw = command(['ip','-j','address','show'])
    if raw is None: return 'NOT VERIFIED'
    try:
        rows = []
        for interface in json.loads(raw):
            for address in interface.get('addr_info', []):
                value = ipaddress.ip_address(address['local'])
                rows.append(dict(address=str(value), prefix=address['prefixlen'], family=value.version,
                                 global_unicast=value.is_global))
        return rows
    except (ValueError, KeyError): return 'NOT VERIFIED'


def routes():
    rows = []
    for family in ('-4','-6'):
        raw = command(['ip','-j',family,'route','show','table','all'])
        if raw is None: return 'NOT VERIFIED'
        try:
            for row in json.loads(raw):
                dst = row.get('dst','default')
                if dst != 'default': dst = str(ipaddress.ip_network(dst, strict=False))
                rows.append(dict(destination=dst, family=family, table=row.get('table','main')))
        except (ValueError, KeyError): return 'NOT VERIFIED'
    return rows


def listeners():
    raw = command(['ss','-H','-lntup'])
    if raw is None: return 'NOT VERIFIED'
    result = []
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) < 5: continue
        endpoint = fields[4]
        address, sep, port = endpoint.rpartition(':')
        if sep and port.isdigit() and fields[0] in ('tcp','udp'):
            if address != '*' and not re.fullmatch(r'[0-9a-fA-F:.\[\]%]+', address): address = 'REDACTED'
            owners = [dict(process=name, pid=int(pid)) for name,pid in re.findall(r'\("([A-Za-z0-9_.-]{1,64})",pid=(\d+),fd=\d+\)', line)]
            result.append(dict(protocol=fields[0], address=address, port=int(port), owners=owners or 'NOT VERIFIED'))
    return result


def self_test():
    canary = 'CANARY_PASSWORD_DO_NOT_REPORT'
    value = directives('server { listen 443 ssl default_server; server_name other.test; proxy_set_header Authorization '+canary+'; ssl_certificate_key /secret/key; }')
    assert canary not in json.dumps(value) and '/secret' not in json.dumps(value)
    assert value['defaults'] == 1 and value['server_name'] == ['other.test']
    assert not DOMAIN.fullmatch('domain;secret') and not DOMAIN.fullmatch('http://memory.test')
    print(json.dumps({'self_test':'PASS','scope':'parser/redaction only; no VPS observed'}))


def main():
    parser = argparse.ArgumentParser(description='Redacted read-only Ubuntu inventory; never deployment approval.')
    parser.add_argument('--domain')
    parser.add_argument('--wg-port', type=int)
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test: return self_test()
    if args.domain and not DOMAIN.fullmatch(args.domain): parser.error('Expected DNS hostname only')
    if args.wg_port is not None and not 1024 <= args.wg_port <= 65535: parser.error('Invalid operator-selected UDP port')
    os_info = {}
    try:
        for line in Path('/etc/os-release').read_text().splitlines():
            key, _, value = line.partition('=')
            if key in ('ID','VERSION_ID'): os_info[key] = re.sub(r'[^a-zA-Z0-9._-]', '', value)
    except OSError: pass
    all_listeners = listeners()
    # nginx -v writes its safe version to stderr; do not emit arbitrary stderr.
    nginx_version = 'NOT VERIFIED'
    if shutil.which('nginx'):
        try:
            value = subprocess.run(['nginx','-v'], capture_output=True, timeout=5)
            found = re.match(rb'\Anginx version: nginx/([0-9.]+)(?:\s|$)', value.stderr)
            if found: nginx_version = found[1].decode('ascii')
        except (OSError, subprocess.TimeoutExpired): pass
    nginx = nginx_summary()
    report = dict(result='NOT VERIFIED', dry_run=True, production_go=False,
        os=os_info or 'NOT VERIFIED', kernel=os.uname().release, openssl=version(['openssl','version']),
        nginx_version=nginx_version, nginx=nginx,
        local_addresses=addresses(), public_nat_ipv4_ipv6='NOT VERIFIED: provider console and routing required',
        routes=routes(), listeners=all_listeners,
        listener_process_ownership='Per-listener owners when available; missing ownership is NOT VERIFIED; command lines not exported',
        selected_domain=args.domain, selected_udp_port=args.wg_port,
        udp_port_locally_busy=any(r['protocol']=='udp' and r['port']==args.wg_port for r in all_listeners) if isinstance(all_listeners,list) and args.wg_port else 'NOT VERIFIED',
        wg_installed=shutil.which('wg') is not None, wg_interfaces='NOT VERIFIED',
        certbot_installed=shutil.which('certbot') is not None,
        acme_certificate_ownership_expiry='NOT VERIFIED: local administrator review without exporting account or private keys',
        firewall=dict(ufw_installed=shutil.which('ufw') is not None, nft_installed=shutil.which('nft') is not None,
                      effective_rules='NOT VERIFIED: privileged IPv4/IPv6 local review required'),
        systemd_conflicts='NOT VERIFIED: select candidate service names before local review',
        shared_ip_compatibility='NOT VERIFIED: full expanded config must be reviewed locally',
        namespace_conflict_in_partial_files=any(r['kulai_namespace'] for r in nginx['partial_files']),
        backups='NOT VERIFIED: operator must identify config backup/restore owner',
        ssh_host_key='NOT VERIFIED: verify independently in provider console before authorizing SSH')
    interfaces = command(['wg','show','interfaces'])
    if interfaces is not None:
        report['wg_interfaces'] = len(interfaces.split())  # No public/private peer keys.
    handshakes = command(['wg','show','all','latest-handshakes'])
    if handshakes is not None:
        values = [int(row.split()[-1]) for row in handshakes.splitlines() if row.split() and row.split()[-1].isdigit()]
        report['wg_handshakes'] = dict(peers=len(values), never=sum(v==0 for v in values),
            ages_seconds=[max(0,int(time.time())-v) for v in values if v])
    state = command(['systemctl','show','--property=ActiveState','--value','nginx.service'])
    report['nginx_service'] = state.strip() if state and state.strip() in ('active','inactive','failed','activating','deactivating') else 'NOT VERIFIED'
    units = command(['systemctl','list-unit-files','--type=service','--no-legend','--no-pager'])
    if units is not None:
        names = [row.split()[0] for row in units.splitlines() if row.split()]
        report['systemd_service_count'] = len(names)
        report['kulai_service_namespace_present'] = any('kulai-memory' in name.lower() for name in names)
    ufw = command(['ufw','status'])
    if ufw is not None:
        status = re.search(r'^Status: (active|inactive)$', ufw, re.M)
        report['firewall']['ufw_status'] = status[1] if status else 'NOT VERIFIED'
    nft = command(['nft','-j','list','tables'])
    if nft is not None:
        try:
            tables = [r['table'] for r in json.loads(nft).get('nftables',[]) if 'table' in r]
            report['firewall']['nft_table_count'] = len(tables)
            report['firewall']['nft_families'] = sorted(set(r['family'] for r in tables if r.get('family') in ('ip','ip6','inet','arp','bridge','netdev')))
        except (ValueError, KeyError, TypeError): pass
    if args.domain:
        resolved = command(['getent','ahosts',args.domain])
        try:
            report['selected_domain_resolves_to'] = sorted(set(str(ipaddress.ip_address(row.split()[0])) for row in resolved.splitlines())) if resolved else 'NOT VERIFIED'
        except ValueError: report['selected_domain_resolves_to'] = 'NOT VERIFIED'
    print(json.dumps(report, indent=2))


if __name__ == '__main__': main()
PY
