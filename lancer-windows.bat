@echo off
setlocal
set "PYTHONPATH="
set "PYTHONHOME="
chcp 65001 >nul

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

echo [Thot] Dossier : %SCRIPT_DIR%

where py >nul 2>nul
if errorlevel 1 (
  where python >nul 2>nul
  if errorlevel 1 (
    echo [Thot] Python 3 est requis ^(>= 3.11^).
    pause
    exit /b 1
  )
)
where node >nul 2>nul
if errorlevel 1 (
  echo [Thot] Node.js est requis ^(>= 22.12.0^).
  pause
  exit /b 1
)
for /f "tokens=* delims=" %%A in ('node --version 2^>nul') do set "NODE_VER=%%A"
rem NODE_VER attendu au format v22.12.0
set "NODE_VER=%NODE_VER:v=%"
for /f "tokens=1,2,3 delims=." %%A in ("%NODE_VER%") do (
  set "NODE_MAJOR=%%A"
  set "NODE_MINOR=%%B"
  set "NODE_PATCH=%%C"
)
if not defined NODE_MAJOR goto :nodeTooOld
if not defined NODE_MINOR goto :nodeTooOld
if %NODE_MAJOR% LSS 22 goto :nodeTooOld
if %NODE_MAJOR% EQU 22 if %NODE_MINOR% LSS 12 goto :nodeTooOld
where npm >nul 2>nul
if errorlevel 1 (
  echo [Thot] npm est requis.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo [Thot] Creation de l'environnement Python...
  where py >nul 2>nul
  if errorlevel 1 (
    python -m venv .venv
  ) else (
    py -3 -m venv .venv
  )
  if errorlevel 1 goto :error
)

set "PYTHON=%SCRIPT_DIR%.venv\Scripts\python.exe"
if not exist ".venv\.thot-deps-installed" (
  echo [Thot] Installation des dependances Python...
  "%PYTHON%" -m pip install --upgrade pip
  if errorlevel 1 goto :error
  "%PYTHON%" -m pip install -r requirements.txt
  if errorlevel 1 goto :error
  type nul > ".venv\.thot-deps-installed"
)

if not exist "node_modules\.thot-deps-installed" goto :installRoot
if not exist "node_modules\electron\dist\electron.exe" goto :installRoot
goto :checkFrontend

:installRoot
if exist "node_modules\.thot-deps-installed" (
  del "node_modules\.thot-deps-installed" || goto :error
)
  echo [Thot] Installation des dependances Node (racine)...
  call npm install
  if errorlevel 1 goto :error
  if not exist "node_modules\electron\dist\electron.exe" (
    echo [Thot] Binaire Electron introuvable apres installation.
    goto :error
  )
  type nul > "node_modules\.thot-deps-installed" || goto :error

:checkFrontend
if not exist "frontend\node_modules\.thot-deps-installed" goto :installFrontend
if not exist "frontend\node_modules\.bin\tsc.cmd" goto :installFrontend
if not exist "frontend\node_modules\.bin\vite.cmd" goto :installFrontend
goto :launch

:installFrontend
if exist "frontend\node_modules\.thot-deps-installed" (
  del "frontend\node_modules\.thot-deps-installed" || goto :error
)
  echo [Thot] Installation des dependances Node (frontend)...
  call npm --prefix frontend install
  if errorlevel 1 goto :error
  for %%B in (tsc vite) do if not exist "frontend\node_modules\.bin\%%B.cmd" (
    echo [Thot] Binaire %%B introuvable apres installation.
    goto :error
  )
  type nul > "frontend\node_modules\.thot-deps-installed" || goto :error

:launch
dir /b /ad "models\*" >nul 2>nul
if errorlevel 1 echo [Thot] Avertissement : aucun modele dans models/. L'interface demarrera, mais l'inference sera indisponible.

set "THOT_DEV_PYTHON=%PYTHON%"
set "THOT_PORTABLE=1"
echo [Thot] Lancement...
call npm start
if errorlevel 1 goto :error
exit /b 0

:nodeTooOld
echo [Thot] Node.js >= 22.12.0 est requis ^(version détectée : %NODE_VER%). Mettez Node.js à jour.
pause
exit /b 1

:error
echo [Thot] Echec. Consultez le message ci-dessus.
pause
exit /b 1
