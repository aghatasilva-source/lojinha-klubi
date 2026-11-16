@echo off
setlocal
cd /d "%~dp0"

if not exist ".tools\node\node.exe" (
  echo Node.js portatil nao encontrado em .tools\node.
  pause
  exit /b 1
)

set "PATH=%CD%\.tools\node;%PATH%"
echo Verificando a API na porta 8000...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$active=@(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -eq 8000 }); if ($active.Count -gt 0) { exit 10 }"
if errorlevel 10 (
  echo API ja esta ativa na porta 8000.
) else (
  echo Iniciando API com recarregamento automatico...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$root=(Get-Location).Path; $python=(Get-Command py -ErrorAction Stop).Source; Start-Process -FilePath $python -ArgumentList '-3','-m','backend.dev_runner' -WorkingDirectory $root -WindowStyle Hidden"
  timeout /t 2 /nobreak >nul
)

echo Verificando a interface na porta 3000...
powershell -NoProfile -ExecutionPolicy Bypass -Command "$active=@(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -eq 3000 }); if ($active.Count -gt 0) { exit 10 }"
if errorlevel 10 (
  echo Interface ja esta ativa na porta 3000.
) else (
  echo Iniciando interface Next.js com atualizacao automatica...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$root=(Get-Location).Path; Start-Process -FilePath $env:ComSpec -ArgumentList '/d','/c','call iniciar_frontend.bat' -WorkingDirectory $root -WindowStyle Hidden"
)
echo LOJINHA KLUBI iniciada. Acesse http://localhost:3000 e use F5 apos salvar alteracoes.
pause
