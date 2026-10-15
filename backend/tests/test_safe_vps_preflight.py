"""Standalone collector redaction under synthetic OS/command evidence."""
import json
from pathlib import Path
import subprocess
import shutil

import pytest

SCRIPT = Path(__file__).resolve().parents[2]/'ops/remote_access/ubuntu/collect-preflight-safe.sh'


def collector():
    source = SCRIPT.read_text(encoding='utf-8').split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
    module = {'__name__':'collector_test'}
    exec(compile(source, str(SCRIPT), 'exec'), module)
    return module


def test_safe_collector_redacts_secret_directives(capsys):
    module = collector()
    module['self_test']()
    assert json.loads(capsys.readouterr().out)['self_test'] == 'PASS'
    value = module['directives']('server_name a.test b.test $secret; listen 443 ssl; proxy_set_header TOKEN supersecret; map $x $kulai_test { default supersecret; }')
    assert value['server_name'] == ['a.test','b.test']
    assert value['kulai_namespace']
    assert 'supersecret' not in json.dumps(value)
    quoted = module['directives']('proxy_set_header X-Test "server_name secret.test; listen 443 default_server;"; server_name real.test;')
    assert quoted['server_name'] == ['real.test'] and quoted['defaults'] == 0


def test_safe_collector_missing_commands_are_not_verified(monkeypatch):
    module = collector()
    monkeypatch.setattr(shutil, 'which', lambda _:None)
    assert module['addresses']() == 'NOT VERIFIED'
    assert module['routes']() == 'NOT VERIFIED'
    assert module['listeners']() == 'NOT VERIFIED'


def test_safe_collector_address_route_and_listener_summaries(monkeypatch):
    module = collector()
    def command(args):
        if args[0] == 'ss': return 'tcp LISTEN 0 511 0.0.0.0:443 0.0.0.0:*\nudp UNCONN 0 0 [::]:51820 [::]:*\n'
        if 'address' in args: return '[{"addr_info":[{"local":"192.0.2.5","prefixlen":32}]}]'
        return '[{"dst":"10.203.71.1/32","table":"main","secret":"not_exported"}]'
    module['command'] = command
    assert module['listeners']()[0] == dict(protocol='tcp',address='0.0.0.0',port=443,owners='NOT VERIFIED')
    assert module['listeners']()[1]['port'] == 51820
    assert module['addresses']()[0]['family'] == 4
    assert 'not_exported' not in json.dumps(module['routes']())


def test_collector_shell_syntax():
    bash = shutil.which('bash') or ('C:/Program Files/Git/bin/bash.exe' if Path('C:/Program Files/Git/bin/bash.exe').exists() else None)
    if not bash: pytest.skip('NOT RUN: shell parser unavailable')
    assert subprocess.run([bash,'-n',str(SCRIPT)],capture_output=True,timeout=10).returncode == 0


def test_complete_collector_report_redacts_private_values(monkeypatch,capsys):
    from types import SimpleNamespace
    import os
    import sys
    module = collector()
    canary = 'PRIVATE_CANARY_DO_NOT_EXPORT'
    module['nginx_summary'] = lambda:dict(partial_files=[],expanded_config='NOT VERIFIED')
    def command(args):
        if args[0] == 'openssl': return 'OpenSSL 3.5.0 '+canary
        if args[:2] == ['wg','show']:
            return 'wg0' if args[-1] == 'interfaces' else 'wg0 '+canary+' 1'
        if args[0] == 'systemctl':
            return 'active' if args[1] == 'show' else canary+'.service enabled\n'
        if args[0] == 'ufw': return 'Status: active\n'+canary
        if args[0] == 'nft': return json.dumps({'nftables':[{'table':{'family':'inet','name':canary}}]})
        if args[0] == 'getent': return '192.0.2.1 STREAM '+canary
        return None
    module['command'] = command
    monkeypatch.setattr(shutil,'which',lambda _: '/mock/bin')
    monkeypatch.setattr(subprocess,'run',lambda *a,**k:SimpleNamespace(stderr=b'nginx version: nginx/1.24.0 (Ubuntu)\n'))
    monkeypatch.setattr(os,'uname',lambda:SimpleNamespace(release='test-kernel'),raising=False)
    monkeypatch.setattr(Path,'read_text',lambda *a,**k:'ID=ubuntu\nVERSION_ID="24.04"\nPRIVATE='+canary)
    monkeypatch.setattr(sys,'argv',['collector','--domain','memory.test','--wg-port','51820'])
    module['main']()
    raw = capsys.readouterr().out
    assert canary not in raw
    value = json.loads(raw)
    assert value['result'] == 'NOT VERIFIED' and value['production_go'] is False
    assert value['nginx_version'] == '1.24.0' and value['firewall']['ufw_status'] == 'active'
    assert value['selected_domain_resolves_to'] == ['192.0.2.1']
