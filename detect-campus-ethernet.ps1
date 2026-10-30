[CmdletBinding()]
param(
    [switch]$AsJson
)

function Get-PhysicalEthernetConnection {
    [CmdletBinding()]
    param(
        [object[]]$Adapters = @(Get-NetAdapter -Physical -ErrorAction Stop)
    )

    @($Adapters | Where-Object {
        $_.Status -eq 'Up' -and
        $_.HardwareInterface -eq $true -and
        $_.PhysicalMediaType -eq '802.3' -and
        $_.LinkSpeed -and
        $_.LinkSpeed -ne '0 bps'
    })
}

try {
    $connections = @(Get-PhysicalEthernetConnection)
    $result = [pscustomobject]@{
        Connected = ($connections.Count -gt 0)
        Adapters  = @($connections | Select-Object Name, InterfaceDescription, Status, LinkSpeed, ifIndex)
        CheckedAt = (Get-Date).ToString('o')
    }

    if ($AsJson) {
        $result | ConvertTo-Json -Depth 4 -Compress
    } elseif ($result.Connected) {
        Write-Output ('Ethernet connected: ' + (($connections | ForEach-Object { $_.Name }) -join ', '))
        exit 0
    } else {
        Write-Output 'No physical Ethernet connection detected.'
        exit 1
    }
} catch {
    Write-Error ('Unable to read adapter state: ' + $_.Exception.Message)
    exit 2
}