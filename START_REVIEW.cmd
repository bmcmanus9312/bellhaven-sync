@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Setting up the demo. This is only needed the first time.
    python -m venv .venv
    if errorlevel 1 goto failed
    .venv\Scripts\python.exe -m pip install -r requirements.txt
    if errorlevel 1 goto failed
)
echo Opening the Bellhaven review app. Keep this window open while using it.
.venv\Scripts\python.exe -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501 --browser.gatherUsageStats false
goto end
:failed
echo Setup failed. Python and an internet connection are needed for first-time setup.
pause
:end
