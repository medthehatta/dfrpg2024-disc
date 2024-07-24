#!/bin/bash
while true
do
    [[ -n "$(git status --porcelain)" ]] || { git fetch origin main && git reset --hard FETCH_HEAD; }
    python ./bot_main.py
done
