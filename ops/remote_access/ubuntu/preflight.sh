#!/bin/sh
# Read-only inventory. Do not run nginx -T, wg showconf, printenv or cat keys.
set -eu
printf '%s\n' '--- OS ---'
cat /etc/os-release
printf '%s\n' '--- Addresses/routes/listeners ---'
ip -brief address
ip route show
ss -lntup
printf '%s\n' '--- nginx version/build, vhost names only ---'
if command -v nginx >/dev/null 2>&1; then nginx -v 2>&1; nginx -V 2>&1; fi
if [ -d /etc/nginx/sites-enabled ]; then ls -l /etc/nginx/sites-enabled; fi
printf '%s\n' '--- Service state ---'
systemctl is-active nginx || true
systemctl list-unit-files 'wg-quick@*' --no-pager || true
printf '%s\n' '--- WireGuard public status (no private/preshared keys) ---'
if command -v wg >/dev/null 2>&1; then
  wg --version
  wg show interfaces
  wg show all latest-handshakes
  wg show all transfer
fi
printf '%s\n' '--- Firewall inventory (privileged read may require operator) ---'
if command -v ufw >/dev/null 2>&1; then ufw status verbose || true; fi
if command -v nft >/dev/null 2>&1; then nft list tables || true; fi
printf '%s\n' 'Full firewall/vhost review must be local, redacted; do not paste keys or nginx -T output.'
