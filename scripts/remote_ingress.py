"""Explicit loopback mTLS launcher. No migrations, public bind or autostart."""
import argparse
import asyncio
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / 'backend/src'))

from kulai_memory.app_factory import create_app
from kulai_memory.remote_ingress import RemoteIngressConfig, RemoteIngressError
from kulai_memory.remote_ingress.server import create_remote_server
from kulai_memory.security import SecurityConfig, SecurityMode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--enable', action='store_true', required=True)
    parser.add_argument('--certificate', type=Path, required=True)
    parser.add_argument('--private-key', type=Path, required=True)
    parser.add_argument('--client-ca', type=Path, required=True)
    parser.add_argument('--expected-gateway-identity', required=True)
    parser.add_argument('--public-origin', required=True)
    parser.add_argument('--gateway-sha256')
    parser.add_argument('--port', type=int, default=8443)
    args = parser.parse_args()
    try:
        config = RemoteIngressConfig(enabled=args.enable, public_origin=args.public_origin,
            expected_gateway_identity=args.expected_gateway_identity,
            certificate=args.certificate, private_key=args.private_key, client_ca=args.client_ca,
            gateway_sha256=args.gateway_sha256)
        app = create_app(security_config=SecurityConfig(mode=SecurityMode.SECURE), remote_ingress_config=config)
        server = create_remote_server(app, port=args.port)
        asyncio.run(server.serve())
        return 0 if server.started else 1
    except Exception:
        print('Remote ingress unavailable; listener not enabled.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
