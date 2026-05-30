#!/bin/bash
while true
do
    [[ -n "$(git status --porcelain)" ]] || { git fetch origin main && git reset --hard FETCH_HEAD; }
    uv run python ./bot_main.py
done
