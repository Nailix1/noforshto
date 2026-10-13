"""Telegram Bot API + управление menu / Mini App URL."""
from __future__ import annotations
import json, urllib.error, urllib.parse, urllib.request
from config import cfg, load, save

VERSION = '940'
_BAD_HOSTS = ('.local', '.localhost', '.ngrok-free.app', '.ngrok-free.dev',
              '.trycloudflare.com', '.ngrok.io')


def public_url(raw: str) -> str:
    raw = str(raw or '').strip().rstrip('/')
    try:
        u = urllib.parse.urlsplit(raw)
        host = (u.hostname or '').lower()
        if (u.scheme != 'https' or not host or u.username or u.password or u.query
                or u.fragment or u.path not in ('', '/') or u.port not in (None, 443)
                or host in ('localhost', '127.0.0.1', '0.0.0.0', '::1')
                or host.endswith(_BAD_HOSTS)):
            return ''
    except ValueError:
        return ''
    return raw


def app_url(base: str, mode: str) -> str:
    if mode not in ('client', 'admin'):
        raise ValueError('Неверный режим')
    base = public_url(base)
    return base + '/?mode=' + mode + '&v=' + VERSION if base else ''


def live_url() -> str:
    return public_url(cfg('miniapp_url', ''))


def api_call(token: str, method: str, data: dict, timeout: int = 12) -> dict:
    if not token:
        return {'ok': False, 'description': 'Отсутствует токен Telegram'}
    req = urllib.request.Request(f'https://api.telegram.org/bot{token}/{method}',
        data=json.dumps(data, ensure_ascii=False).encode(), method='POST',
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode('utf-8'))
        except (ValueError, OSError):
            body = {}
        exc.close()
        return {'ok': False,
                'description': str(body.get('description') or 'Telegram API error').replace(token, '[REDACTED]')[:200]}
    except (OSError, ValueError):
        return {'ok': False, 'description': 'Сеть или Telegram API недоступны'}


def set_menu(token: str, url: str):
    if not token or not url:
        return False, 'Токен или адрес отсутствует'
    desired = {'type': 'web_app', 'text': '🕸 Открыть Mini App', 'web_app': {'url': url}}
    r = api_call(token, 'setChatMenuButton', {'menu_button': desired})
    if not r.get('ok'):
        return False, str(r.get('description', 'Кнопка не установлена'))[:180]
    check = api_call(token, 'getChatMenuButton', {})
    got = check.get('result') or {}
    if not (check.get('ok') and got.get('type') == 'web_app'
            and (got.get('web_app') or {}).get('url') == url):
        return False, 'Telegram не подтвердил сохранённый адрес'
    return True, 'Подтверждено'


def health(url: str, timeout: int = 12) -> bool:
    url = public_url(url)
    if not url:
        return False
    req = urllib.request.Request(url + '/api/health', headers={'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200 or 'application/json' not in r.headers.get('Content-Type', ''):
                return False
            resp = json.load(r)
        return resp.get('ok') is True and resp.get('booking_mode') == 'direct'
    except (ValueError, OSError, urllib.error.URLError):
        return False


def activate(base: str) -> dict:
    base = public_url(base)
    if not base:
        return {'ready': False, 'reason': 'Нужен постоянный HTTPS-домен'}
    if not health(base):
        return {'ready': False, 'reason': 'Публичный /api/health не отвечает'}
    conf = load()
    client, cmsg = set_menu(conf.get('client_bot_token'), app_url(base, 'client'))
    admin, amsg = set_menu(conf.get('bot_token'), app_url(base, 'admin'))
    if client:
        conf['miniapp_url'] = base
        save(conf)
    return {'ready': client and admin, 'client': client, 'admin': admin,
            'client_status': cmsg, 'admin_status': amsg,
            'reason': '' if client and admin else 'Одна из кнопок не подтверждена'}