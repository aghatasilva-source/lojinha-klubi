@echo off
setlocal
cd /d "%~dp0frontend"
set "PATH=%~dp0.tools\node;%PATH%"
call "%~dp0.tools\node\npm.cmd" run dev
