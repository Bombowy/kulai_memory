"""Evaluate aggregate observations only. No URLs, bodies, keys or credentials.

Input is an operator/metrics-agent snapshot. Missing observations remain UNKNOWN;
this does not restart services, open gates, probe private APIs or reconnect writes.
"""
import argparse
import json
from pathlib import Path


def evaluate(data):
    alerts=[]
    def flag(name, good): alerts.append({'check':name,'status':'UNKNOWN' if good is None else 'OK' if good else 'ALERT'})
    def number(key, minimum, maximum):
        value=data.get(key)
        return minimum<=value<=maximum if type(value) in (int,float) else None
    for field in ('windows_reachable','bridge_listening','nginx_upstream_available','https_probe_ok','wss_probe_ok'):
        flag(field,data.get(field) if type(data.get(field)) is bool else None)
    flag('wg_handshake_fresh',number('wg_handshake_age_seconds',0,180))
    flag('certificate_validity',number('certificate_days_remaining',7,825))
    flag('gate_open',data.get('gate_state')=='OPEN' if 'gate_state' in data else None)
    flag('snapshot_fresh',number('snapshot_age_seconds',0,120))
    for status in ('401','403','429','503'):
        count=data.get('status_counts',{}).get(status)
        flag('status_'+status,0<=count<=20 if type(count) is int else None)
    for field in ('refresh_uncertain_count','session_error_count'):
        flag(field,number(field,0,0))
    return {'result':'ALERT' if any(a['status']=='ALERT' for a in alerts) else
            'NOT VERIFIED' if any(a['status']=='UNKNOWN' for a in alerts) else 'HEALTHY',
            'automatic_action':False,'checks':alerts}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot',type=Path,required=True)
    args=parser.parse_args()
    try:
        if args.snapshot.stat().st_size>65536: raise ValueError()
        output=evaluate(json.loads(args.snapshot.read_text(encoding='utf-8-sig')))
    except Exception: output={'result':'NOT VERIFIED','automatic_action':False}
    print(json.dumps(output))
    return 0 if output['result']=='HEALTHY' else 2

if __name__=='__main__': raise SystemExit(main())
