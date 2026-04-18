
@echo off
setlocal

python -m pip install --upgrade pip
python -m pip install -r requirements.txt

python -m PyInstaller --noconfirm --clean --onefile --windowed --name "FinancieraNuevoProgreso" main.py

echo.
echo Hecho. Busca el EXE en dist\FinancieraNuevoProgreso.exe
pause
