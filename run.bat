@echo off
cd /d "%~dp0"
if not exist .venv (
  echo Pehli baar setup ho raha hai, thoda wait karo...
  py -m venv .venv
  call .venv\Scripts\activate
  pip install -r requirements.txt
) else (
  call .venv\Scripts\activate
)
py app.py
pause
