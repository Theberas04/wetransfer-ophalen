@echo off
setlocal
cd /d "%~dp0"

rem Bouw de Windows-versie. Werkt lokaal precies zoals in CI.
py -3.12 -m venv .venv
if errorlevel 1 goto :error
call .venv\Scripts\activate
python -m pip install --upgrade pip
if errorlevel 1 goto :error
python -m pip install -r requirements.txt -r requirements-dev.txt pyinstaller
if errorlevel 1 goto :error

rem Het app-pictogram wordt uit het logobestand gemaakt, zodat Windows en de
rem website hetzelfde merkteken gebruiken.
python tools\brand_icon.py
if errorlevel 1 goto :error

pyinstaller --noconfirm --clean --windowed --name "WeTransfer Ophalen" ^
  --icon "branding\app.ico" ^
  --add-data "ui;ui" ^
  --add-data "branding;branding" ^
  --collect-all webview ^
  --collect-all pythonnet ^
  --collect-all clr_loader ^
  --hidden-import webview.platforms.edgechromium ^
  main.py
if errorlevel 1 goto :error

rem Windows zet op alles wat uit een gedownloade ZIP komt het "Mark of the Web".
rem De .NET-runtime weigert die bestanden dan te laden, waardoor de app meteen
rem stopt. Deze config naast de .exe geeft ze volledig vertrouwen.
copy /Y "packaging\WeTransfer Ophalen.exe.config" "dist\WeTransfer Ophalen\WeTransfer Ophalen.exe.config" >nul
if errorlevel 1 goto :error

echo.
echo Build klaar: dist\WeTransfer Ophalen\WeTransfer Ophalen.exe
echo Distributie: lever de volledige map dist\WeTransfer Ophalen mee (of de ZIP uit CI).
exit /b 0

:error
echo Build mislukt. Controleer de foutmelding hierboven.
exit /b 1
