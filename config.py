"""Загрузка config.json и переменных окружения Railway."""
from __future__ import annotations
import json, os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / 'config.json'
DATA_DIR = Path(os.environ.get('NEFOR_DATA_DIR', str(ROOT))).resolve()
WEB_DIR = ROOT / 'web'

_ENV_MAP = {
    'bot_token': 'NEFOR_ADMIN_BOT_TOKEN',
    'client_bot_token': 'NEFOR_CLIENT_BOT_TOKEN',
    'admin_id': 'NEFOR_ADMIN_ID',
    'tetradka_token': 'NEFOR_TETRADKA_TOKEN',
    'miniapp_url': 'NEFOR_MINIAPP_URL',
    'salon_address': 'NEFOR_SALON_ADDRESS',
    'booking_url': 'NEFOR_BOOKING_URL',
}


def load() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    return json.loads(CONFIG_FILE.read_text(encoding='utf-8-sig'))


def save(cfg: dict) -> None:
    tmp = CONFIG_FILE.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(tmp, CONFIG_FILE)


def cfg(key: str, default=None):
    env_key = _ENV_MAP.get(key)
    if env_key and os.environ.get(env_key):
        value = os.environ[env_key]
        if key == 'admin_id':
            try:
                return int(value)
            except ValueError:
                return default
        if key == 'miniapp_port':
            try:
                return int(value)
            except ValueError:
                return default
        if key == 'booking_enabled':
            return value.lower() in ('1', 'true', 'yes')
        return value
    return load().get(key, default)


def admin_id() -> int:
    return int(cfg('admin_id') or 0)


def bot_token(mode: str) -> str:
    return str(cfg('bot_token' if mode == 'admin' else 'client_bot_token') or '')