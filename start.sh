#!/bin/bash

while true; do
    git fetch origin main && git checkout FETCH_HEAD
    python bot_main.py
    sleep 0.5
done
