#!/usr/bin/env python3
"""DigiNeighbour Bot — entry point"""
import os, sys
sys.path.insert(0, os.path.dirname(__file__))

from core.db import init_db, q
from core.bot import run_bot

if __name__ == "__main__":
    os.makedirs("logs", exist_ok=True)
    os.makedirs(os.path.dirname(os.environ.get("DN_DB", "/app/data/dn.db")), exist_ok=True)
    init_db()
    token = os.environ.get("BOT_TOKEN", "")
    if not token:
        community = q("SELECT bot_token,name FROM communities WHERE is_active=1 AND bot_token IS NOT NULL LIMIT 1", one=True)
        token = community["bot_token"] if community else ""
    if not token:
        print("\n❌ No bot token. Set BOT_TOKEN environment variable or add via dashboard.\n")
        sys.exit(1)
    print(f"\n✅ Starting DigiNeighbour bot...\n")
    run_bot(token)
