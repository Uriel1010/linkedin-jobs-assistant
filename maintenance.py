"""Daily consistent SQLite snapshots, private and limited to seven days."""
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def backup_database(source, folder, prefix, keep=7):
    source, folder = Path(source), Path(folder)
    if not re.fullmatch(r'[a-z][a-z0-9-]*',prefix):
        raise ValueError('Invalid backup prefix')
    if not source.exists():
        return False
    folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    day = datetime.now(timezone.utc).date().isoformat()
    target = folder/f'{prefix}-{day}.sqlite3'
    if target.exists():
        return False
    temporary = folder/f'{prefix}-{day}.tmp'
    src = sqlite3.connect(str(source),timeout=20)
    dst = sqlite3.connect(str(temporary),timeout=20)
    try:
        src.backup(dst)
    finally:
        dst.close(); src.close()
    temporary.chmod(0o600)
    temporary.replace(target)
    for old in sorted(folder.glob(f'{prefix}-????-??-??.sqlite3'),reverse=True)[keep:]:
        old.unlink()
    return True
