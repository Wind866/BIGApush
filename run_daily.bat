@echo off
chcp 65001 >nul
cd /d D:\KHunter
set PYTHONIOENCODING=utf-8
python main.py run >> logs\daily_run.log 2>&1
