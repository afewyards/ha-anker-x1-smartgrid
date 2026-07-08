#!/usr/bin/env sh
exec uvicorn server:app --host 0.0.0.0 --port 8099
