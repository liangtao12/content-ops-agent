"""LangGraph checkpoint 持久化。"""
import sqlite3
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from . import config


def create_checkpointer(path: Path | None = None) -> SqliteSaver:
    checkpoint_path = path or config.CHECKPOINT_FILE
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(checkpoint_path),
        check_same_thread=False,
        timeout=30,
    )
    return SqliteSaver(conn)
