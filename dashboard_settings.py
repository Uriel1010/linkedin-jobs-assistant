"""Validated dashboard access to the bot's shared preferences, never its credentials."""
import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from feed_rules import DEFAULT_PREFERENCES, ROLES, LEVELS

router = APIRouter()
KEYS = ('preferences', 'quiet_hours', 'notify')


class Preferences(BaseModel):
    model_config = ConfigDict(extra='forbid')
    roles: list[str] = Field(min_length=1, max_length=16)
    levels: list[str] = Field(min_length=1, max_length=8)
    region: str
    max_age_hours: int = Field(ge=1, le=168)
    keep_unknown_level: bool
    other_scope: str
    include: list[str] = Field(max_length=20)
    exclude: list[str] = Field(max_length=20)

    @field_validator('roles', 'levels')
    @classmethod
    def choices(cls, value, info):
        if not set(value) <= (ROLES if info.field_name == 'roles' else LEVELS):
            raise ValueError('Unknown choice.')
        return list(dict.fromkeys(value))

    @field_validator('region', 'other_scope')
    @classmethod
    def scope(cls, value, info):
        if value not in (('israel', 'broad') if info.field_name == 'region' else ('technical', 'all')):
            raise ValueError('Unknown scope.')
        return value

    @field_validator('include', 'exclude')
    @classmethod
    def keywords(cls, value):
        if any(not v.strip() or len(v) > 100 for v in value):
            raise ValueError('Use keywords of 1–100 characters.')
        return list(dict.fromkeys(v.strip().casefold() for v in value))


class QuietHours(BaseModel):
    model_config = ConfigDict(extra='forbid')
    enabled: bool
    start: int = Field(ge=0, lt=1440)
    end: int = Field(ge=0, lt=1440)
    timezone: str = Field(max_length=100)

    @field_validator('timezone')
    @classmethod
    def zone(cls, value):
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError('Use a valid timezone, such as Asia/Jerusalem.')
        return value

    @model_validator(mode='after')
    def distinct(self):
        if self.enabled and self.start == self.end:
            raise ValueError('Quiet hours must start and end at different times.')
        return self


class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: str = Field(max_length=64)
    preferences: Preferences
    quiet_hours: QuietHours
    notify: bool


@contextmanager
def connection():
    demo = os.environ.get('DASHBOARD_DEMO') == '1'
    if demo:
        path = Path(os.environ.get('DASHBOARD_STATE_DIR', '/app/state')) / 'demo-settings.sqlite3'
        con = sqlite3.connect(path, timeout=10)
        con.execute('CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
        con.commit()
    else:
        path = Path(os.environ.get('BOT_STATE_DB', '/app/bot-state/bot.sqlite3'))
        if not path.is_file():
            raise HTTPException(503, 'Bot settings are unavailable. Check the bot state volume.')
        con = sqlite3.connect(path.as_uri() + '?mode=rw', uri=True, timeout=10)
    try:
        with con:
            yield con, demo
    except sqlite3.Error:
        raise HTTPException(503, 'Settings are busy or unavailable. Please try again.') from None
    finally:
        con.close()


def snapshot(con):
    values = {k: json.loads(v) for k, v in con.execute(
        "SELECT key,value FROM settings WHERE key IN ('preferences','quiet_hours','notify')")}
    revision = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
    return {
        'preferences': {**DEFAULT_PREFERENCES, **values.get('preferences', {})},
        'quiet_hours': {'enabled': False, 'start': 1380, 'end': 480,
                        'timezone': 'Asia/Jerusalem', **values.get('quiet_hours', {})},
        'notify': values.get('notify', False), 'revision': revision,
    }


@router.get('/api/settings')
def read_settings():
    with connection() as (con, demo):
        return snapshot(con)


@router.put('/api/settings')
def save_settings(body: Settings):
    with connection() as (con, demo):
        con.execute('BEGIN IMMEDIATE')
        current = snapshot(con)
        if body.revision != current['revision']:
            raise HTTPException(409, 'Settings changed in Telegram or another tab. Reload settings before saving.')
        if body.notify and not demo:
            initialized = con.execute("SELECT value FROM settings WHERE key='feed_initialized'").fetchone()
            if not initialized or not json.loads(initialized[0]):
                raise HTTPException(409, 'Start the Telegram feed with /notify on once before enabling it here.')
        values = body.model_dump(exclude={'revision'})
        if values['preferences'] != current['preferences'] and not demo:
            # Match the bot's preference update semantics, in the same transaction.
            # Already-sent jobs retain their delivery records and are not replayed.
            con.execute('DELETE FROM delivery_parts WHERE sent_at IS NULL')
            con.execute("UPDATE deliveries SET batch_id=NULL WHERE status='pending'")
            con.execute("UPDATE deliveries SET status='pending',reason=NULL WHERE status='filtered'")
        con.executemany('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                        [(k, json.dumps(values[k])) for k in KEYS])
        return snapshot(con)
