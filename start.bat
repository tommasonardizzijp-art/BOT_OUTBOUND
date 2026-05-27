@echo off
echo ====================================
echo   BOT OUTBOUND - Avvio sistema
echo ====================================
echo.

:: Check Memurai/Redis with a short retry window. Memurai can need a few
:: seconds after Windows boot before it accepts connections.
set "MEMURAI_CLI=D:\Memurai\memurai-cli.exe"
echo [1/5] Verifica Redis/Memurai...
for /L %%I in (1,1,10) do (
    if exist "%MEMURAI_CLI%" (
        "%MEMURAI_CLI%" -h 127.0.0.1 -p 6379 ping >nul 2>&1
        if not errorlevel 1 goto redis_ok
    ) else (
        powershell -NoProfile -Command "$c = New-Object Net.Sockets.TcpClient; try { $c.Connect('127.0.0.1', 6379); exit 0 } catch { exit 1 } finally { $c.Close() }" >nul 2>&1
        if not errorlevel 1 goto redis_ok
    )
    if %%I LSS 10 (
        echo Redis non pronto, nuovo tentativo tra 2 secondi...
        timeout /t 2 /nobreak >nul
    )
)

echo [ATTENZIONE] Redis non risponde sulla porta 6379.
echo Verifica che il servizio Memurai sia attivo:
echo   - Apri "Servizi" di Windows e cerca "Memurai"
echo   - Oppure esegui: "%MEMURAI_CLI%" -h 127.0.0.1 -p 6379 ping
pause
exit /b 1

:redis_ok
echo [1/5] Redis gia' in esecuzione (Memurai). OK
echo.

:: Run migrations
echo [2/5] Applico migrazioni database...
pushd "%~dp0backend"
call venv\Scripts\activate
python -m scripts.migrate
if errorlevel 1 (
    echo [ERRORE] Migrazioni fallite. Avvio annullato.
    popd
    pause
    exit /b 1
)
popd
echo.

:: Start Backend
echo [3/5] Avvio backend FastAPI (porta 8000)...
start "BOT OUTBOUND - Backend" cmd /k "cd /d %~dp0backend && venv\Scripts\activate && uvicorn app.main:app --reload --port 8000"
timeout /t 3 /nobreak >nul

:: Start ARQ Worker
echo [4/5] Avvio ARQ worker DM...
start "BOT OUTBOUND - Worker" cmd /k "cd /d %~dp0backend && venv\Scripts\activate && arq app.workers.task_queue.WorkerSettings"
timeout /t 2 /nobreak >nul

:: Start Cron Worker
echo [5/5] Avvio ARQ cron worker...
start "BOT OUTBOUND - Cron Worker" cmd /k "cd /d %~dp0backend && venv\Scripts\activate && arq app.workers.cron_worker.CronWorkerSettings"
timeout /t 2 /nobreak >nul

:: Start Frontend
echo [extra] Avvio frontend Next.js (porta 3000)...
start "BOT OUTBOUND - Frontend" cmd /k "cd /d %~dp0frontend && npm run dev"

echo.
echo ====================================
echo   Sistema avviato!
echo   Dashboard: http://localhost:3000
echo   API docs:  http://localhost:8000/docs
echo ====================================
echo.
pause
