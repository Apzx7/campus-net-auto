. (Join-Path $PSScriptRoot '..\detect-campus-ethernet.ps1')

function Assert-Equal($actual, $expected, $message) {
    if ($actual -ne $expected) {
        throw "$message. Expected: '$expected'; Actual: '$actual'"
    }
}

$wired = [pscustomobject]@{ Name = 'Ethernet'; Status = 'Up'; HardwareInterface = $true; PhysicalMediaType = '802.3'; LinkSpeed = '1 Gbps'; ifIndex = 12 }
$wifi = [pscustomobject]@{ Name = 'WLAN'; Status = 'Up'; HardwareInterface = $true; PhysicalMediaType = 'Native 802.11'; LinkSpeed = '300 Mbps'; ifIndex = 23 }
$virtual = [pscustomobject]@{ Name = 'VMware Network Adapter'; Status = 'Up'; HardwareInterface = $false; PhysicalMediaType = '802.3'; LinkSpeed = '100 Mbps'; ifIndex = 3 }
$disconnected = [pscustomobject]@{ Name = 'Ethernet'; Status = 'Disconnected'; HardwareInterface = $true; PhysicalMediaType = '802.3'; LinkSpeed = '0 bps'; ifIndex = 12 }

Assert-Equal (@(Get-PhysicalEthernetConnection -Adapters @($wired)).Count) 1 'Connected Ethernet should be detected'
Assert-Equal (@(Get-PhysicalEthernetConnection -Adapters @($wifi)).Count) 0 'Wi-Fi should not count as Ethernet'
Assert-Equal (@(Get-PhysicalEthernetConnection -Adapters @($virtual)).Count) 0 'Virtual adapter should not count'
Assert-Equal (@(Get-PhysicalEthernetConnection -Adapters @($disconnected)).Count) 0 'Disconnected Ethernet should not count'

Write-Output 'All Ethernet detection tests passed.'