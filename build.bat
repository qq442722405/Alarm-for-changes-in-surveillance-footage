@echo off
chcp 65001 >nul
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
pyinstaller --noconfirm --clean --windowed --onefile --icon=app.ico --add-data "yolo11n.pt;." --name="监控变化报警" main.py
pause
