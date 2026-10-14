from dataclasses import replace
from datetime import datetime,timezone
from pathlib import Path
import pytest
from backend.tests.test_remote_gateway_config import deployment
from backend.tests.remote_tls_fixtures import certificates
from ops.remote_access.validator import validate,nginx_inventory,certificate
from ops.remote_access.monitor import evaluate


EXISTING='''http {
 server { listen 192.0.2.10:443 ssl default_server; server_name _; ssl_reject_handshake on; }
 server { listen 192.0.2.10:443 ssl; server_name other.test; }
}'''


def test_missing_inventory_never_returns_success():
    output=validate(replace(deployment(),shared_ip=True),{})
    assert output['result']=='NOT VERIFIED' and output['production_go'] is False


@pytest.mark.parametrize('replace_from,replace_to',[
    ('server_name other.test','server_name memory.test'),
    ('server_name other.test','server_name *.test'),
    (' ssl default_server',' ssl'),
    ('listen 192.0.2.10:443 ssl;','listen 192.0.2.10:443;'),
    ('http {','http { map $x $kulai_host_ok { default 0; }'),
    ('http {','http { limit_req_zone $x zone=kulai_requests:1m rate=1r/s;'),
    (' ssl default_server',' ssl default_server proxy_protocol'),
])
def test_shared_ownership_conflicts_fail_closed(replace_from,replace_to):
    config=replace(deployment(),shared_ip=True)
    assert nginx_inventory(config,EXISTING)
    assert not nginx_inventory(config,EXISTING.replace(replace_from,replace_to))


def test_dedicated_listener_refuses_existing_ownership():
    assert not nginx_inventory(deployment(),EXISTING)
    shared=replace(deployment(),shared_ip=True).candidates()['kulai-memory.nginx.conf']
    assert ' ssl default_server;' not in shared
    assert 'proxy_ssl_verify on;' in shared and 'if ($kulai_sni_ok = 0)' in shared


def full_inventory():
    return dict(observed_at=datetime.now(timezone.utc).isoformat(),nginx_expanded=True,combined_nginx_t_ok=True,
        ubuntu_firewall_peer_only=True,windows_firewall_peer_only=True,private_services_loopback=True,
        private_key_acl_ok=True,rollback_reviewed=True,bridge_interface_and_port_ok=True,
        gateway_identity_configured=True,safe_restart_reviewed=True,default_tls_12_13_only=True,
        certificate_key_match=True,gate_outside_database_backup=True,expected_gateway_identity='gateway.test',lan_vpn_routes=['192.168.1.0/24'],
        backend=dict(mode='SECURE',canonical_head='kulai_memory_0004',security_head='kulai_security_0003',
                     revocation_available=True,gate_state='CLOSED',external_seal_verified=True),
        wireguard=dict(ubuntu_allowed_ips=['10.203.71.2/32'],windows_allowed_ips=['10.203.71.1/32'],
                       ubuntu_address='10.203.71.1/32',windows_address='10.203.71.2/32',persistent_keepalive=25))


@pytest.mark.parametrize('section,key,value',[
    ('backend','mode','LOCAL_DEV'),('backend','gate_state','OPEN'),
    ('backend','security_head','kulai_security_0002'),('backend','canonical_head','kulai_memory_0003'),
    ('backend','external_seal_verified',False),('wireguard','windows_allowed_ips',['0.0.0.0/0']),
    (None,'lan_vpn_routes',['10.203.0.0/16']), (None,'private_services_loopback',False),
    (None,'observed_at','2000-01-01T00:00:00+00:00'),(None,'certificate_key_match',False),
])
def test_bad_production_evidence_blocks(section,key,value):
    inventory=full_inventory()
    (inventory[section] if section else inventory)[key]=value
    assert validate(replace(deployment(),shared_ip=True),inventory,existing_nginx=EXISTING)['result']=='BLOCKED'


def test_certificate_checks_actual_chain_san_purpose_and_expiry(certificates):
    openssl=Path('C:/Program Files/Git/usr/bin/openssl.exe')
    if not openssl.is_file(): pytest.skip('NOT RUN: isolated offline OpenSSL unavailable')
    ca=certificates.path('ca','pem')
    assert certificate(certificates.path('gateway','pem'),'gateway.test',purpose='ssl_client',ca=ca,openssl=openssl,minimum_days=0)
    for name in ('foreign','wrong-san','expired','wrong-purpose'):
        assert not certificate(certificates.path(name,'pem'),'gateway.test',purpose='ssl_client',ca=ca,openssl=openssl,minimum_days=0)
    assert not certificate(certificates.path('gateway','pem'),'gateway.test',purpose='ssl_client',ca=ca,openssl=openssl)


def test_fully_attested_review_and_redacted_output(certificates,tmp_path):
    config=replace(deployment(),shared_ip=True)
    for name,body in config.candidates().items(): (tmp_path/name).write_text(body,encoding='utf-8')
    # Real isolated PKI only; no system trust-store changes.
    certificates.ca,certificates.ca_key=certificates.root('ca',days=60)
    for name,identity,server in [('public','memory.test',True),('ingress','ingress.test',True),('long-gateway','gateway.test',False)]:
        certificates.make(name,identity=identity,server=server,days=30)
    specs={key:dict(certificate=str(certificates.path(name,'pem')),ca=str(certificates.path('ca','pem')))
           for key,name in [('public','public'),('ingress','ingress'),('gateway','long-gateway')]}
    output=validate(config,full_inventory(),existing_nginx=EXISTING,candidates=tmp_path,
                    certificates=specs,openssl='C:/Program Files/Git/usr/bin/openssl.exe')
    assert output['result']=='READY FOR REVIEW' and not output['production_go']
    assert str(tmp_path) not in str(output) and 'memory.test' not in str(output)


def test_monitor_missing_and_failed_observations_are_not_healthy():
    assert evaluate({})['result']=='NOT VERIFIED'
    data=dict(windows_reachable=True,bridge_listening=True,nginx_upstream_available=True,
              https_probe_ok=True,wss_probe_ok=True,wg_handshake_age_seconds=20,certificate_days_remaining=30,
              gate_state='OPEN',snapshot_age_seconds=10,status_counts={'401':0,'403':0,'429':0,'503':0},
              refresh_uncertain_count=0,session_error_count=0)
    assert evaluate(data)['result']=='HEALTHY'
    for key,value in [('gate_state','CLOSED'),('wg_handshake_age_seconds',181),('certificate_days_remaining',1),('refresh_uncertain_count',1)]:
        assert evaluate({**data,key:value})['result']=='ALERT'
    data['access_token']='synthetic-do-not-output'
    assert 'synthetic-do-not-output' not in str(evaluate(data))
