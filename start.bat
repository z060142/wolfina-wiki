@echo off
if exist .env (
    for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
        if not "%%A"=="" if not "%%A:~0,1%"=="#" set "%%A=%%B"
    )
)

if not defined SERVER_HOST set SERVER_HOST=127.0.0.1
if not defined SERVER_PORT set SERVER_PORT=8000

uv run uvicorn api.app:app --host %SERVER_HOST% --port %SERVER_PORT%
