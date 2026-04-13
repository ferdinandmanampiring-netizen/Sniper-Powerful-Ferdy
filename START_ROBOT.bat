
@echo off
title SNIPER POWERFUL FERDY - LIVE CONSOLE
color 0A
taskkill /f /im python.exe >nul 2>&1
echo --------------------------------------------------
echo [SYSTEM] Membersihkan sisa sesi lama...
del sniper_session.session-journal >nul 2>&1
echo [SYSTEM] Memulai Sniper Powerful Ferdy di Jendela Baru...
echo --------------------------------------------------
:: Menjalankan di jendela baru agar tidak mengganggu Cursor
start powershell.exe -NoExit -Command "python 05_main_orchestrator.py"
exit
