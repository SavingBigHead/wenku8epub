@echo off
wsl.exe -d Arch --cd /home/meng/wenku8epub --exec uv run --locked run.py %*
pause
