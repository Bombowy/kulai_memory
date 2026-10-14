"""Explicit co-hosted WireGuard TCP bridge + loopback verified ingress. No DDL."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend/src'))

from kulai_memory.app_factory import create_app
from kulai_memory.remote_ingress import RemoteIngressConfig
from kulai_memory.remote_ingress.server import create_remote_server
from kulai_memory.remote_ingress.tcp_bridge import BridgeConfig, serve_with_bridge
from kulai_memory.security import SecurityConfig, SecurityMode


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--enable',action='store_true')
    args=parser.parse_args()
    try:
        data=json.loads(args.config.read_text(encoding='utf-8-sig'))
        ingress=dict(data['ingress'])
        for key in ('certificate','private_key','client_ca'):
            ingress[key]=Path(ingress[key])
        config=RemoteIngressConfig(enabled=True,**ingress)
        bridge=BridgeConfig(**data['bridge'])
        if bridge.test_loopback:
            raise ValueError()  # Test fixtures, never production launcher fallback.
        config.tls_context()
        if not args.enable:
            print('Configuration valid; dry-run. No listener, DB writes or system changes.')
            return 0
        app=create_app(security_config=SecurityConfig(mode=SecurityMode.SECURE),remote_ingress_config=config)
        ingress=create_remote_server(app,port=8443)
        asyncio.run(serve_with_bridge(app,ingress,bridge))
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        print('Remote bridge unavailable; no fallback.',file=sys.stderr)
        return 1

if __name__=='__main__':
    raise SystemExit(main())
