@echo off
wsl.exe -d Arch --cd /home/meng/wenku8epub --exec .venv/bin/python run.py %*
pause
