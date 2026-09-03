@echo off
chcp 65001 >nul
setlocal
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 goto :error
pyinstaller --noconfirm --clean --windowed --onefile --icon=app.ico --add-data "app.ico;." --name "屏幕监控智能报警" main.py
if errorlevel 1 goto :error
echo.
echo ========================================
echo 打包成功：dist\屏幕监控智能报警.exe
echo ========================================
pause
exit /b 0
:error
echo.
echo 打包失败，请查看上面的错误信息。
pause
exit /b 1
