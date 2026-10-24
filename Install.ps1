<#
.SYNOPSIS
  Instala a ponte Muse <-> Claude Code v2 como tarefa agendada do Windows.
.DESCRIPTION
  - Cria a tarefa "MuseBridge": inicia no logon, reinicia sozinha se cair,
    nao abre janela de terminal, nao inicia segunda instancia se ja houver uma.
  - Mata instancias v1 (queue_server.py / session_server.py / runner.ps1).
  - Inicia a tarefa na hora e verifica /health.
  Uso:
    powershell -ExecutionPolicy Bypass -File Install.ps1 [-RepoDir C:\projects\nootrieapp] [-PythonExe python]
    powershell -ExecutionPolicy Bypass -File Install.ps1 -Uninstall
  Licao v6 (nao quebrar): NUNCA usar $ErrorActionPreference = "Stop" neste
  script — com "Stop", qualquer stderr de comando nativo vira erro fatal e
  nenhum redirect salva. Usar "Continue" + checagem explicita de $LASTEXITCODE.
#>
param(
  [string]$RepoDir = "C:\projects\nootrieapp",
  [string]$PythonExe = "python",
  [switch]$Uninstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

$TaskName = "MuseBridge"
$InstallDir = $PSScriptRoot
$ServerPy = Join-Path $InstallDir "server.py"

if ($Uninstall) {
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Out-Null
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
  Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*muse-bridge\server.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Write-Host "MuseBridge desinstalado."
  exit 0
}

Write-Host "== Instalando MuseBridge (v2) =="
Write-Host "Pasta: $InstallDir"

# 1. Python?
& $PythonExe --version 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
  Write-Host "ERRO: Python nao encontrado como '$PythonExe'. Instale o Python 3 e rode de novo."
  exit 1
}

# 2. IP Tailscale (o servidor escuta SOMENTE nele)
$ip = $null
$ts = Get-Command tailscale -ErrorAction SilentlyContinue
if ($ts) {
  $out = & tailscale ip -4 2>$null
  if ($out) { $ip = "$out".Trim().Split("`n")[0].Trim() }
}
if (-not $ip) {
  $addr = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -like "100.*" } |
    Select-Object -First 1 -ExpandProperty IPAddress
  if ($addr) { $ip = $addr }
}
if (-not $ip) {
  Write-Host "ERRO: nao achei o IP Tailscale. Abra o app Tailscale, conecte, e rode de novo."
  exit 1
}
Write-Host "IP Tailscale: $ip"

# 3. Mata instancias v1 (uma vez, na instalacao)
Write-Host "-- parando instancias v1 ..."
try {
  Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*queue_server.py*" -or $_.CommandLine -like "*session_server.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*runner.ps1*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  Start-Sleep -Seconds 2
} catch { }

# 4. Cria (ou recria) a tarefa agendada
Write-Host "-- criando tarefa agendada '$TaskName' ..."
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue | Out-Null
$action = New-ScheduledTaskAction -Execute $PythonExe `
  -Argument "`"$ServerPy`" `"$RepoDir`" $ip" -WorkingDirectory $InstallDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -RestartCount 3 `
  -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action `
  -Trigger $trigger -Settings $settings -Force | Out-Null
if ($LASTEXITCODE -ne 0) {
  Write-Host "ERRO: nao consegui criar a tarefa agendada."
  exit 1
}

# 5. Inicia agora e verifica
Start-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Out-Null
Start-Sleep -Seconds 4
try {
  $h = Invoke-WebRequest -Uri "http://${ip}:8000/health" -TimeoutSec 10 -UseBasicParsing
  Write-Host "OK: $($h.Content)"
} catch {
  Write-Host "AVISO: /health ainda nao respondeu — aguarde alguns segundos e abra http://${ip}:8000/transcript"
}

Write-Host ""
Write-Host "Pronto. A ponte inicia sozinha a cada logon, sem terminal aberto."
Write-Host "Transcricao ao vivo: http://${ip}:8000/transcript"
Write-Host "Desinstalar: powershell -ExecutionPolicy Bypass -File Install.ps1 -Uninstall"
