"""Candidate-policy and bridge admission tests; no system/network writes."""
from dataclasses import replace
from pathlib import Path
import subprocess
import os

import pytest

from kulai_memory.remote_ingress import RemoteIngressError
from kulai_memory.remote_ingress.tcp_bridge import BridgeConfig
from ops.remote_access.package import Deployment
from backend.tests.remote_tls_fixtures import certificates


def deployment():
    return Deployment('https://memory.test','192.0.2.10','10.203.71.2','10.203.71.1',
        'vps.test','ingress.test','/p/public.pem','/p/public.key','/p/gateway.pem',
        '/p/gateway.key','/p/ca.pem','/p/safe.log')


@pytest.mark.parametrize('changes',[
    {'public_origin':'http://memory.test'}, {'public_origin':'https://memory.test:0'},
    {'public_origin':'https://memory.test:'}, {'public_origin':'https://memory.test?'},
    {'public_origin':'https://memory.test/'}, {'public_origin':'https://secret@memory.test'},
    {'public_listen_address':'0.0.0.0'}, {'windows_wg_ip':'127.0.0.1'},
    {'vps_wg_ip':'10.203.71.2'}, {'ingress_identity':'*.test'},
    {'gateway_private_key':'/p/key;proxy_ssl_verify off'}, {'bridge_port':80000},
    {'test_listen_port':12345},
])
def test_production_config_rejects_downgrade_wildcard_and_injection(changes):
    with pytest.raises(ValueError): replace(deployment(),**changes)


@pytest.mark.parametrize('before,after',[
    ('proxy_ssl_verify on','proxy_ssl_verify off'),
    ('proxy_pass https://','proxy_pass http://'),
    ('proxy_next_upstream off','proxy_next_upstream error timeout'),
    ('proxy_pass_request_headers off','proxy_pass_request_headers on'),
    ('proxy_ssl_name ingress.test','proxy_ssl_name evil.test'),
    ('proxy_cache off','proxy_cache insecure'),
    ('proxy_set_header Host "memory.test"','proxy_set_header Host $http_host'),
])
def test_weakened_nginx_candidate_is_refused(tmp_path,before,after):
    config=deployment()
    for name,body in config.candidates().items(): (tmp_path/name).write_text(body,encoding='utf-8')
    config.validate_candidates(tmp_path)
    path=tmp_path/'kulai-memory.nginx.conf'
    original=path.read_text(encoding='utf-8')
    assert before in original
    path.write_text(original.replace(before,after),encoding='utf-8')
    with pytest.raises(ValueError): config.validate_candidates(tmp_path)


def test_wireguard_exact_peer_only_no_home_routing_or_real_keys():
    config=deployment()
    files=config.candidates()
    windows=files['kulai-memory.windows.conf']
    ubuntu=files['kulai-memory.ubuntu.conf']
    assert 'AllowedIPs = 10.203.71.1/32' in windows
    assert 'AllowedIPs = 10.203.71.2/32' in ubuntu
    assert 'PersistentKeepalive = 25' in windows and 'Endpoint = vps.test:51820' in windows
    assert '<VPS_PRIVATE_KEY_FROM_PROTECTED_STORAGE>' in ubuntu
    assert '<WINDOWS_PRIVATE_KEY_FROM_PROTECTED_STORAGE>' in windows
    for body in (windows,ubuntu):
        assert '0.0.0.0/0' not in body and 'PostUp' not in body.replace('# Dedicated kulai-memory tunnel. No SaveConfig, PostUp or routing/NAT hooks.','')


@pytest.mark.parametrize('changes',[
    {'listen_address':'0.0.0.0'},{'listen_address':'192.0.2.10'},
    {'peer_address':'0.0.0.0'},{'peer_address':'10.203.71.2'},
    {'interface_alias':'Wi-Fi;cmd'}, {'interface_alias':''},{'max_connections':33},
    {'idle_timeout':0},{'lifetime':901},{'port':0},{'test_loopback':True},
])
def test_bridge_rejects_wildcard_nonprivate_unbounded_and_mislabelled_test(changes):
    with pytest.raises(RemoteIngressError):
        replace(BridgeConfig('10.203.71.2','10.203.71.1','kulai-memory'),**changes)


def test_powershell_package_parses_without_execution():
    if os.name!='nt': pytest.skip('Windows PowerShell parser not available')
    root=Path(__file__).resolve().parents[2]
    code="param($root); $errors=@(); Get-ChildItem -LiteralPath $root -Recurse -Filter '*.ps1' | ForEach-Object { $tokens=$null; $problems=$null; [System.Management.Automation.Language.Parser]::ParseFile($_.FullName,[ref]$tokens,[ref]$problems) | Out-Null; $errors+=@($problems) }; if ($errors.Count -gt 0) { $errors | ForEach-Object { Write-Output $_.Message }; exit 1 }"
    command=['powershell.exe','-NoProfile','-NonInteractive','-Command','& { '+code+' }',str(root/'ops/remote_access')]
    result=subprocess.run(command,capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr


def test_windows_firewall_complement_excludes_only_expected_peer():
    if os.name!='nt': pytest.skip('Windows PowerShell not available')
    script=Path(__file__).resolve().parents[2]/'ops/remote_access/windows/bridge.ps1'
    # Extract only the pure address-range function; never run firewall cmdlets.
    code=("param($file); $ast=[System.Management.Automation.Language.Parser]::ParseFile($file,[ref]$null,[ref]$null); "
          "$fn=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Other-IPv4Ranges'},$true); "
          ". ([scriptblock]::Create($fn.Extent.Text)); Other-IPv4Ranges '10.203.71.1' | ConvertTo-Json -Compress")
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command','& { '+code+' }',str(script)],
                          capture_output=True,text=True,timeout=15)
    assert result.returncode==0
    import json
    assert json.loads(result.stdout)==['0.0.0.0-10.203.71.0','10.203.71.2-255.255.255.255']


@pytest.mark.parametrize('scenario,expected',[
    ('verify','accepted'),('setup','accepted'),('rollback','accepted'),
    ('conflict','refused'),('wrong_filter','refused'),('wrong_adapter','refused'),
])
def test_windows_bridge_policy_dry_run_with_mocked_os_inventory(scenario,expected):
    if os.name!='nt': pytest.skip('Windows PowerShell not available')
    root=Path(__file__).resolve().parents[2]
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','RemoteSigned',
        '-File',str(root/'backend/tests/bridge_policy_harness.ps1'),'-Scenario',scenario,
        '-BridgeScript',str(root/'ops/remote_access/windows/bridge.ps1')],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()==expected,result.stderr


@pytest.mark.parametrize('gateway,identity,ok',[
    ('gateway','gateway.test',True),('gateway','wrong.test',False),
    ('foreign','gateway.test',False),('expired','gateway.test',False),
    ('wrong-purpose','gateway.test',False),
])
def test_offline_certificate_validation_uses_chain_purpose_and_exact_san(certificates,gateway,identity,ok):
    openssl=Path('C:/Program Files/Git/usr/bin/openssl.exe')
    if os.name!='nt' or not openssl.is_file(): pytest.skip('Offline Windows OpenSSL diagnostic not available')
    script=Path(__file__).resolve().parents[2]/'ops/remote_access/check-certificates.ps1'
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-ExecutionPolicy','RemoteSigned',
        '-File',str(script),'-OpenSsl',str(openssl),'-Ca',str(certificates.path('ca','pem')),
        '-IngressCertificate',str(certificates.path('server','pem')),'-IngressIdentity','ingress.test',
        '-GatewayCertificate',str(certificates.path(gateway,'pem')),'-GatewayIdentity',identity,
        '-MinimumValiditySeconds','3600'],
        capture_output=True,text=True,timeout=15)
    assert (result.returncode==0)==ok, result.stdout+result.stderr
