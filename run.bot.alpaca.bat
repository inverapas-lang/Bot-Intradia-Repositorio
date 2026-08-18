@echo off
REM Supervisor externo para bot_alpaca.py (analogo a run.bot.bat, para el
REM bot de IBKR).
REM
REM El bot tiene un "vigilante" interno que fuerza su propio cierre si se
REM queda congelado sin dar señales de vida durante 20 minutos (ver
REM UMBRAL_CONGELACION_SEGUNDOS en bot_alpaca.py). Ese vigilante NO puede
REM reiniciar el proceso por si solo, porque vive dentro del mismo proceso
REM que se acaba de matar. Este script hace de supervisor externo: si el
REM bot termina por cualquier motivo (se congela y se autocierra, se cae
REM por un error no controlado, etc.), lo vuelve a lanzar automaticamente.
REM
REM Uso: haz doble clic en este archivo (o ejecutalo desde CMD) en vez de
REM lanzar "python bot_alpaca.py" directamente. Asegurate de tener puestas
REM las variables de entorno ALPACA_API_KEY / ALPACA_SECRET_KEY antes (ver
REM ALPACA_NOTES.md).
REM
REM Para detener el bot del todo: cierra esta ventana de CMD, o pulsa
REM Ctrl+C y luego responde que NO quieres que continue el bucle (Ctrl+C
REM otra vez si te pregunta "Terminar el trabajo por lotes (S/N)?").

:bucle
echo.
echo ============================================================
echo Iniciando bot_alpaca.py  (%DATE% %TIME%)
echo ============================================================
python bot_alpaca.py

echo.
echo El bot se ha detenido. Reiniciando en 10 segundos...
echo (Cierra esta ventana o pulsa Ctrl+C dos veces para no reiniciar)
timeout /t 10
goto bucle
