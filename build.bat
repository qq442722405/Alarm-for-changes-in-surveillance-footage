@echo off
chcp 65001 >nul
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
pyinstaller --noconfirm --clean --windowed --onefile --icon=app.ico --add-data "app.ico;." --name "屏幕监控智能报警" main.py
echo.
echo ========================================
echo 打包完成：
echo dist\屏幕监控智能报警.exe
echo ========================================
pause
