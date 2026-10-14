param([Parameter(Mandatory=$true)][string]$Directory)
$ErrorActionPreference='Stop'
try {
    $owners=@('S-1-5-18','S-1-5-32-544',[Security.Principal.WindowsIdentity]::GetCurrent().User.Value)
    # OWNER RIGHTS grants rights only to the independently checked owner.
    $allowed=$owners + @('S-1-3-0','S-1-3-4')
    $paths=@($Directory) + @(Get-ChildItem -LiteralPath $Directory -Force -File | ForEach-Object FullName)
    foreach ($path in $paths) {
        $acl=Get-Acl -LiteralPath $path
        $owner=$acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
        if ($owner -notin $owners) { throw 'Untrusted owner' }
        foreach ($rule in $acl.GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])) {
            if ($rule.AccessControlType -eq 'Allow' -and $rule.IdentityReference.Value -notin $allowed) { throw 'Broad directory access' }
        }
    }
    Write-Output 'PRIVATE'
} catch { Write-Error 'Private recovery directory ACL verification failed'; exit 1 }
