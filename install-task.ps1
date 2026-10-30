# 注册计划任务：开机登录后自动连一次校园网，连上就退出（不常驻、不留后台进程）
# 用法：powershell -ExecutionPolicy Bypass -File .\install-task.ps1
# 可选：-IntervalMinutes 5   （默认 0 = 只在登录时跑一次；填数字则额外每 N 分钟自检）
#       -TaskName "CampusNet-AutoConnect"

[CmdletBinding()]
param(
    [string]$TaskName = 'CampusNet-AutoConnect',
    [int]$IntervalMinutes = 0
)

$ErrorActionPreference = 'Stop'

$root = $PSScriptRoot
$script = Join-Path $root 'connect.py'
if (-not (Test-Path $script)) { throw "找不到 connect.py：$script" }

$config = Join-Path $root 'config.json'
if (-not (Test-Path $config)) {
    throw "还没有 config.json。请先双击「改账号密码.bat」，或运行：python connect.py --setup"
}

# 优先用 pythonw.exe：后台静默运行，不弹黑窗口
$pw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pw) {
    $py = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
    if (-not $py) { throw '找不到 Python，请确认已安装并加入 PATH' }
    $pw = $py
}
Write-Host "Python : $pw"
Write-Host "脚本   : $script"

$action = New-ScheduledTaskAction -Execute $pw `
    -Argument ('"{0}" --quiet --boot' -f $script) `
    -WorkingDirectory $root

# 触发场景：开机/登录、休眠唤醒、网络变成"已连接"（插网线/切换网络）
# 延迟调到很小：脚本内部有快速探测 + wait_ready_sec 兜底，不靠任务延迟等待
function New-DelayedLogonTrigger {
    $t = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $t.Delay = 'PT3S'
    return $t
}

function New-EventTrigger([string]$log, [string]$provider, [int]$id, [string]$delay) {
    # 用 XPath 订阅事件；ValueQueries 用于把事件里的数据带进来（这里不需要）
    $xpath = "*[System[Provider[@Name='$provider'] and EventID=$id]]"
    $cls = Get-CimClass -ClassName MSFT_TaskEventTrigger -Namespace Root/Microsoft/Windows/TaskScheduler
    $t = New-CimInstance -CimClass $cls -ClientOnly
    $t.Enabled = $true
    $t.Subscription = "<QueryList><Query Id='0' Path='$log'><Select Path='$log'>$xpath</Select></Query></QueryList>"
    $t.Delay = $delay
    return $t
}

$triggers = @()
$triggers += New-DelayedLogonTrigger

# 休眠/睡眠唤醒（Power-Troubleshooter 事件 1）
try {
    $triggers += New-EventTrigger 'System' 'Microsoft-Windows-Power-Troubleshooter' 1 'PT3S'
    Write-Host "已启用触发器：休眠唤醒"
} catch { Write-Warning "唤醒触发器添加失败：$($_.Exception.Message)" }

# 网络从断开变成已连接（插网线、切换网络后触发）
try {
    $triggers += New-EventTrigger 'Microsoft-Windows-NetworkProfile/Operational' 'Microsoft-Windows-NetworkProfile' 10000 'PT2S'
    Write-Host "已启用触发器：网络已连接"
} catch { Write-Warning "网络触发器添加失败：$($_.Exception.Message)" }

if ($IntervalMinutes -gt 0) {
    # 可选：额外周期性自检（已在线时会 1 秒内退出）
    $repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(2) `
        -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
        -RepetitionDuration (New-TimeSpan -Days 3650)
    $triggers += $repeat
}

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "已存在同名任务，先移除旧任务"
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
    -Settings $settings -Description '开机连校园网：检测到网线且未认证时自动登录一次，成功后退出' | Out-Null

Write-Host ""
Write-Host "已注册计划任务：$TaskName" -ForegroundColor Green
Write-Host "触发方式：登录 Windows 后 15 秒执行一次（连上即结束进程）"
if ($IntervalMinutes -gt 0) {
    Write-Host ("另外每 {0} 分钟自检一次" -f $IntervalMinutes)
}
Write-Host ""
Write-Host "立即试跑：Start-ScheduledTask -TaskName $TaskName"
Write-Host "查看结果：Get-ScheduledTaskInfo -TaskName $TaskName"
Write-Host "卸载    ：powershell -ExecutionPolicy Bypass -File .\uninstall-task.ps1"
