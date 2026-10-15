# Read-only. No elevation, firewall/service/configuration mutations or queries.
$ErrorActionPreference='Stop'
$result=[ordered]@{dry_run=$true;port=5432;listeners=@();clients=@();profiles=@();effective_rules='NOT VERIFIED'}
try {
    $result.listeners=@(Get-NetTCPConnection -State Listen -LocalPort 5432 -ErrorAction Stop | ForEach-Object {
        $process=Get-Process -Id $_.OwningProcess -ErrorAction Stop
        [pscustomobject]@{address=$_.LocalAddress;pid=$_.OwningProcess;process=$process.ProcessName}
    })
    $result.clients=@(Get-NetTCPConnection -RemotePort 5432 -State Established -ErrorAction Stop | ForEach-Object {
        $process=Get-Process -Id $_.OwningProcess -ErrorAction Stop
        [pscustomobject]@{local_address=$_.LocalAddress;server_address=$_.RemoteAddress;pid=$_.OwningProcess;process=$process.ProcessName}
    })
} catch { $result.network_observation='NOT VERIFIED: some process/socket data inaccessible' }
try {
    $result.profiles=@(Get-NetFirewallProfile -PolicyStore ActiveStore -ErrorAction Stop | Select-Object Name,Enabled,DefaultInboundAction,DefaultOutboundAction)
    $filters=@(Get-NetFirewallPortFilter -PolicyStore ActiveStore -ErrorAction Stop | Where-Object {
        $_.Protocol -in @('TCP','6','Any') -and @($_.LocalPort | Where-Object {
            $_ -eq 'Any' -or $_ -eq '5432' -or ($_ -match '^(\d+)-(\d+)$' -and [int]$Matches[1] -le 5432 -and [int]$Matches[2] -ge 5432)
        }).Count -gt 0
    })
    $allow=0;$block=0
    foreach ($filter in $filters) {
        foreach ($rule in @(Get-NetFirewallRule -AssociatedNetFirewallPortFilter $filter -PolicyStore ActiveStore -ErrorAction Stop)) {
            if ($rule.Enabled -eq 'True' -and $rule.Direction -eq 'Inbound') {
                if ($rule.Action -eq 'Allow') {$allow++} elseif ($rule.Action -eq 'Block') {$block++}
            }
        }
    }
    $result.effective_rules=[ordered]@{matching_inbound_allow_count=$allow;matching_inbound_block_count=$block;
        interpretation='Candidate port matches only; administrator must review scope, application, service, interface and IPv6 filters locally. Counts do not prove isolation.'}
} catch { $result.effective_rules='NOT VERIFIED: privileged ActiveStore port/rule filter read required; no elevation attempted' }
$result | ConvertTo-Json -Depth 5
