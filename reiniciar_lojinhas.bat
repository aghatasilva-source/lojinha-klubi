@echo off
setlocal
cd /d "%~dp0"

echo Encerrando somente os processos da LOJINHA KLUBI...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$all=@(Get-CimInstance Win32_Process); $patterns='backend\.dev_runner|backend\.app\.main|frontend.*run.*dev|next.*dev.*3000'; $processes=@($all | Where-Object { $_.CommandLine -and $_.CommandLine -match $patterns }); $ports=@(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -in @(3000,8000) }); $portProcesses=@($all | Where-Object { $_.ProcessId -in @($ports.OwningProcess) -and $_.CommandLine -and $_.CommandLine -match $patterns }); $ids=@($processes.ProcessId)+@($portProcesses.ProcessId); $ids | Where-Object { $_ -and $_ -ne $PID } | Sort-Object -Unique | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }; Start-Sleep -Seconds 1"

echo Iniciando a aplicacao novamente...
call "%~dp0iniciar_lojinhas.bat"
