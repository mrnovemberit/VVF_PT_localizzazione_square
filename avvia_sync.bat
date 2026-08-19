@echo off
rem Avvia il sincronizzatore interventi usando il Python dell'ambiente virtuale
rem creato nella cartella del progetto. E' questo il file da indicare come
rem azione nell'Utilita' di pianificazione di Windows: cosi' l'attivita' non
rem dipende da quale Python sia nel PATH dell'utente.

cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo Ambiente virtuale non trovato in .venv
    echo Crealo con:  python -m venv .venv
    echo poi installa: .venv\Scripts\python.exe -m pip install -r requirements-sync.txt
    exit /b 1
)

rem pythonw.exe non apre nessuna finestra: i messaggi finiscono in
rem sync_interventi.log, nella stessa cartella.
start "" ".venv\Scripts\pythonw.exe" "sync_interventi.py"
