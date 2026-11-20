#!/bin/sh
# Restart this checkout's services with independent process sessions.
cd "$(dirname "$0")" || exit 1
exec .venv/bin/python desk.py "$@"
