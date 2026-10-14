param([string]$PythonExecutable)
$ErrorActionPreference='Stop'
# Read only; deliberately no environment, service command lines or WireGuard config.
Get-NetAdapter | Select-Object Name,InterfaceDescription,Status,ifIndex
Get-NetIPAddress -AddressFamily IPv4 | Select-Object InterfaceAlias,IPAddress,PrefixLength
Get-NetRoute -AddressFamily IPv4 | Select-Object InterfaceAlias,DestinationPrefix,NextHop,RouteMetric
Get-NetTCPConnection -State Listen | Select-Object LocalAddress,LocalPort,OwningProcess
Get-NetFirewallProfile | Select-Object Name,Enabled,DefaultInboundAction,DefaultOutboundAction
Get-NetFirewallRule -Name 'KulAI-Memory-Bridge-v1-*' -ErrorAction SilentlyContinue | Select-Object Name,Enabled,Direction,Action,Profile
Get-Service | Where-Object { $_.Name -match 'WireGuard|postgres|KulAI' } | Select-Object Name,Status,StartType
Get-Command wireguard.exe,wg.exe -ErrorAction SilentlyContinue | Select-Object Name,Source
powercfg /a
powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE
if ($PythonExecutable) {
    # Operator selects the configured backend SDK; no package install.
    & $PythonExecutable -c "import sys, importlib.metadata; print(sys.version.split()[0]); print('uvicorn='+importlib.metadata.version('uvicorn'))"
    if ($LASTEXITCODE -ne 0) { throw 'Runtime inventory unavailable' }
}
Write-Output 'NOT VERIFIED: VPS, tunnel handshake, firewall Apply and public phone smoke require separate authorization.'
