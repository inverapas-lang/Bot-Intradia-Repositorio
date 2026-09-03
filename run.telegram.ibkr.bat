@echo off
REM Arranca telegram_bot_ibkr.py (control por Telegram del bot de IBKR).
REM
REM ANTES DE USARLO: sustituye los dos valores de abajo (TU_TOKEN_AQUI y
REM TU_CHAT_ID_AQUI) por los tuyos reales -ver NOTES.md, seccion "Control
REM por Telegram del bot de IBKR", para como conseguirlos con @BotFather-.
REM Puedes reutilizar el mismo bot de Telegram que ya tengas para Alpaca (un
REM chat puede hablar con varios bots distintos sin problema) o crear uno
REM nuevo.
REM
REM Uso: haz doble clic en este archivo (o ejecutalo desde CMD), en una
REM ventana APARTE de la que este corriendo run.bot.bat. Dejalo abierto
REM mientras quieras poder controlar el bot desde el movil.
REM
REM Si se cae por cualquier motivo (corte de red con Telegram, etc.) este
REM script lo vuelve a lanzar automaticamente, igual que run.bot.bat hace
REM con el bot de trading.

set TELEGRAM_BOT_TOKEN=TU_TOKEN_AQUI
set TELEGRAM_CHAT_ID=TU_CHAT_ID_AQUI

:bucle
echo.
echo ============================================================
echo Iniciando telegram_bot_ibkr.py  (%DATE% %TIME%)
echo ============================================================
python telegram_bot_ibkr.py

echo.
echo Se ha detenido. Reiniciando en 10 segundos...
echo (Cierra esta ventana o pulsa Ctrl+C dos veces para no reiniciar)
timeout /t 10
goto bucle
