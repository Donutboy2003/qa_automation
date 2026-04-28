@echo off
echo Installing QA Automation Tool...

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed. Please download and install it from https://www.python.org/downloads/
    pause
    exit /b 1
)

echo Creating virtual environment...
python -m venv venv

echo Installing dependencies...
call venv\Scripts\activate
pip install -r requirements.txt

echo Installing Chromium browser...
playwright install chromium

echo.
echo Installation complete. Run run.bat to start the tool.
pause