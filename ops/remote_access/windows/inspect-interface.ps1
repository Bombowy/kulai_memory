param(
    [Parameter(Mandatory=$true)][string]$InterfaceAlias,
    [Parameter(Mandatory=$true)][string]$Address,
    [Parameter(Mandatory=$true)][string]$PeerAddress
)
$ErrorActionPreference = 'Stop'
try {
    if ($InterfaceAlias -notmatch '^[A-Za-z0-9_-]{1,64}$') { throw 'Invalid interface alias' }
    $localIp = [System.Net.IPAddress]::Parse($Address)
    $peerIp = [System.Net.IPAddress]::Parse($PeerAddress)
    if ($localIp.AddressFamily -ne 'InterNetwork' -or $peerIp.AddressFamily -ne 'InterNetwork' -or $Address -eq $PeerAddress) { throw 'IPv4 peer pair required' }
    $adapter = Get-NetAdapter -Name $InterfaceAlias -ErrorAction Stop
    if ($adapter.Status -ne 'Up' -or $adapter.InterfaceDescription -notmatch 'WireGuard|Wintun') { throw 'Dedicated WireGuard interface required' }
    $addresses = @(Get-NetIPAddress -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4)
    if ($addresses.Count -ne 1 -or $addresses[0].IPAddress -ne $Address -or $addresses[0].PrefixLength -ne 32) { throw 'Unexpected tunnel addressing' }
    $route = @(Get-NetRoute -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 | Where-Object { $_.DestinationPrefix -eq ($PeerAddress + '/32') })
    if ($route.Count -ne 1) { throw 'Exact peer route missing' }
    $broadRoutes = @(Get-NetRoute -InterfaceIndex $adapter.ifIndex -AddressFamily IPv4 | Where-Object { $_.DestinationPrefix -eq '0.0.0.0/0' })
    if ($broadRoutes.Count -gt 0) { throw 'Default tunnel route prohibited' }
    [pscustomobject]@{ verified = $true; interface = $InterfaceAlias } | ConvertTo-Json -Compress
} catch {
    Write-Error 'WireGuard interface verification failed (no changes made).'
    exit 1
}
