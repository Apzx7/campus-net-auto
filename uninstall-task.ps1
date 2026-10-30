# 卸载计划任务
# 用法：powershell -ExecutionPolicy Bypass -File .\uninstall-task.ps1

[CmdletBinding()]
param([string]$TaskName = 'CampusNet-AutoConnect')

$ErrorActionPreference = 'Stop'

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "没有找到计划任务 $TaskName，无需卸载。"
    return
}

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "已删除计划任务：$TaskName" -ForegroundColor Green
