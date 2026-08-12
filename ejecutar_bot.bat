@echo off
REM Supervisor externo para bot_completo.py.
REM
REM El bot tiene un "vigilante" interno que fuerza su propio cierre si se
REM queda congelado sin dar señales de vida durante 20 minutos (ver
REM UMBRAL_CONGELACION_SEGUNDOS en bot_completo.py). Ese vigilante NO puede
REM reiniciar el proceso por si solo, porque vive dentro del mismo proceso
REM que se acaba de matar. Este script hace de supervisor externo: si el
REM bot termina por cualquier motivo (se congela y se autocierra, se cae
REM por un error no controlado, etc.), lo vuelve a lanzar automaticamente.
REM
REM Uso: haz doble clic en este archivo (o ejecutalo desde CMD) en vez de
REM lanzar "python bot_completo.py" directamente.
REM
REM Para detener el bot del todo: cierra esta ventana de CMD, o pulsa
REM Ctrl+C y luego responde que NO quieres que continue el bucle (Ctrl+C
REM otra vez si te pregunta "Terminar el trabajo por lotes (S/N)?").

:bucle
echo.
echo ============================================================
echo Iniciando bot_completo.py  (%DATE% %TIME%)
echo ============================================================
python bot_completo.py

echo.
echo El bot se ha detenido. Reiniciando en 10 segundos...
echo (Cierra esta ventana o pulsa Ctrl+C dos veces para no reiniciar)
timeout /t 10
goto bucle
