#!/bin/bash

# Start aria2 RPC daemon
aria2c --enable-rpc --rpc-listen-all --rpc-allow-origin-all --rpc-listen-port=6800 -D

# Install Python dependencies
pip install -r requirements.txt

# Run the bot
python bot.py