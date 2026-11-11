@echo off
cd /d "d:\soft\点名软件\RELAY"
for /f %%i in ('dir /b /o:-d relay_server_v*.exe') do (set RELAY_EXE=%%i & goto run)
set RELAY_EXE=relay_server.exe
:run
%RELAY_EXE% >> relay.out.log 2>> relay.err.log
