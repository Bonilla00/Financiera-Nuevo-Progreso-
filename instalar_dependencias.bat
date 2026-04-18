@echo off
echo Instalando dependencias...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
echo.
echo Listo. Puedes ejecutar la aplicacion con:
echo     python main.py
pause
