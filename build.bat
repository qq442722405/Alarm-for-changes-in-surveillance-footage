@echo off
chcp 65001 >nul
python -m pip install -r requirements.txt
python -m PyInstaller --noconfirm --clean --windowed --onefile --name "监控回放智能监控" main.py
echo.
echo 打包完成：dist\监控回放智能监控.exe
pause
