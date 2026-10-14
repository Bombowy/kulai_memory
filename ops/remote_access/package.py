"""Render reviewed, separate candidate files. Default: validation only, no repair.

No SSH, DDL, service management or system configuration is performed here.
"""
import argparse
from dataclasses import dataclass
from ipaddress import IPv4Address, ip_network
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent


def dns(value):
    return isinstance(value, str) and len(value) <= 253 and all(
        re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in value.split('.'))


@dataclass(frozen=True)
class Deployment:
    public_origin: str
    public_listen_address: str
    windows_wg_ip: str
    vps_wg_ip: str
    vps_endpoint: str
    ingress_identity: str
    public_certificate: str
    public_private_key: str
    gateway_certificate: str
    gateway_private_key: str
    ingress_ca: str
    access_log: str
    wireguard_port: int = 51820
    bridge_port: int = 8443
    test_loopback: bool = False
    test_listen_port: int | None = None
    shared_ip: bool = False

    def __post_init__(self):
        if type(self.test_loopback) is not bool or type(self.shared_ip) is not bool:
            raise ValueError('Explicit boolean test mode required.')
        origin = urlsplit(self.public_origin)
        if (not dns(origin.hostname or '') or origin.scheme != 'https' or origin.path or origin.query or
                origin.fragment or origin.username is not None or origin.password is not None or
                self.public_origin != 'https://' + origin.netloc or origin.netloc !=
                origin.hostname + (':' + str(origin.port) if origin.port is not None else '')):
            raise ValueError('Invalid explicit public HTTPS origin.')
        if origin.port is not None and not 1 <= origin.port <= 65535:
            raise ValueError('Invalid port.')
        if self.test_listen_port is not None and (not self.test_loopback or type(self.test_listen_port) is not int or not 1 <= self.test_listen_port <= 65535):
            raise ValueError('Test listener override requires isolated loopback.')
        local, windows, vps = map(IPv4Address, (self.public_listen_address, self.windows_wg_ip, self.vps_wg_ip))
        if self.test_loopback:
            if not all(ip.is_loopback for ip in (local, windows, vps)):
                raise ValueError('Test listeners require loopback.')
        else:
            private = [ip_network(net) for net in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16')]
            if local.is_unspecified or local.is_loopback or windows == vps or not all(any(ip in net for net in private) for ip in (windows,vps)):
                raise ValueError('Explicit distinct private tunnel IPs required.')
        if not dns(self.ingress_identity) or not (dns(self.vps_endpoint) or self.vps_endpoint == str(vps)):
            raise ValueError('Explicit certificate identity / endpoint required.')
        for port in (self.bridge_port,self.wireguard_port):
            if type(port) is not int or not 1 <= port <= 65535:
                raise ValueError('Invalid port.')
        for value in (self.public_certificate,self.public_private_key,self.gateway_certificate,
                      self.gateway_private_key,self.ingress_ca,self.access_log):
            if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_./:\\-]+',value) or not (value.startswith('/') or re.match(r'^[A-Za-z]:[/\\]',value)):
                raise ValueError('Absolute paths without Nginx metacharacters required.')

    def candidates(self):
        origin = urlsplit(self.public_origin)
        port = origin.port if origin.port is not None else 443
        replacements = {
            'PUBLIC_HOST':origin.hostname, 'PUBLIC_AUTHORITY':origin.netloc,
            'DEFAULT_AUTHORITY':(f'    "{origin.hostname}:443" 1;' if origin.port is None else
                                 f'    "{origin.hostname}" 1;' if origin.port == 443 else ''),
            'LISTEN':f'{self.public_listen_address}:{self.test_listen_port or port}',
            'UPSTREAM':f'{self.windows_wg_ip}:{self.bridge_port}',
            'INGRESS_IDENTITY':self.ingress_identity,
            'PUBLIC_CERT':self.public_certificate, 'PUBLIC_KEY':self.public_private_key,
            'GATEWAY_CERT':self.gateway_certificate, 'GATEWAY_KEY':self.gateway_private_key,
            'INGRESS_CA':self.ingress_ca,'ACCESS_LOG':self.access_log,
            'NULL_DEVICE':'nul' if self.test_loopback and __import__('os').name == 'nt' else '/dev/null',
            'VPS_WG_IP':self.vps_wg_ip,'WINDOWS_WG_IP':self.windows_wg_ip,
            'WG_UDP_PORT':str(self.wireguard_port),'VPS_ENDPOINT':self.vps_endpoint,
            'DEFAULT_SERVER': '' if self.shared_ip else '''server {
    listen @@LISTEN@@ ssl default_server;
    server_name _;
    ssl_reject_handshake on;
    access_log off;
    error_log @@NULL_DEVICE@@ emerg;
    return 421;
}''',
        }
        result = {}
        for source, target in (('nginx/kulai-memory.conf.in','kulai-memory.nginx.conf'),
                               ('wireguard/ubuntu.conf.in','kulai-memory.ubuntu.conf'),
                               ('wireguard/windows.conf.in','kulai-memory.windows.conf')):
            body = (ROOT / source).read_text(encoding='utf-8')
            body = body.replace('@@DEFAULT_SERVER@@', replacements['DEFAULT_SERVER'])
            for key,value in replacements.items():
                body = body.replace('@@'+key+'@@',value.replace('\\','/'))
            if '@@' in body:
                raise ValueError('Unresolved placeholder.')
            result[target] = body
        return result

    def validate_candidates(self, directory):
        # Exact policy provenance, not a grep that accepts a later overriding off.
        # This deliberately refuses hand-edited or weakened candidates.
        for name,expected in self.candidates().items():
            source = directory / name
            if source.is_symlink() or source.read_text(encoding='utf-8') != expected:
                raise ValueError('Candidate differs from reviewed policy.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--write-candidates',type=Path)
    parser.add_argument('--validate-candidates',type=Path)
    args=parser.parse_args()
    try:
        config=Deployment(**json.loads(args.config.read_text(encoding='utf-8-sig')))
        candidates=config.candidates()
        if args.validate_candidates:
            config.validate_candidates(args.validate_candidates)
        if args.write_candidates:
            # Exclusive, new directory only; never overwrite vhosts/services.
            args.write_candidates.mkdir(parents=False,exist_ok=False)
            for name,body in candidates.items():
                with (args.write_candidates/name).open('x',encoding='utf-8',newline='\n') as output:
                    output.write(body)
        print(json.dumps({'validated':True,'dry_run':not bool(args.write_candidates),
                          'files':list(candidates),'production_applied':False}))
        return 0
    except Exception:
        print('Deployment candidate validation failed; no system changes.')
        return 1

if __name__=='__main__':
    raise SystemExit(main())
