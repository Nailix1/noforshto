"""Очередь уведомлений: Telegram account → client bot → ручная SMS."""
from __future__ import annotations
import asyncio, hashlib, time
from datetime import datetime, timedelta, timezone

from storage import db, norm_phone, linked_telegram_id, NOTIFY_DB

MSK = timezone(timedelta(hours=3))


def initialize():
    with db(NOTIFY_DB) as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS messages (
            key TEXT PRIMARY KEY, visit_id TEXT NOT NULL, kind TEXT NOT NULL,
            day TEXT NOT NULL, at_time TEXT NOT NULL, phone TEXT NOT NULL DEFAULT '',
            telegram_id INTEGER, text TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
            channel TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '',
            provider_id TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS message_state_idx ON messages(state, created_at);
        CREATE TABLE IF NOT EXISTS snapshot (
            visit_id TEXT PRIMARY KEY, day TEXT NOT NULL, at_time TEXT NOT NULL,
            phone TEXT NOT NULL, master TEXT NOT NULL, service TEXT NOT NULL,
            name TEXT NOT NULL, is_done INTEGER NOT NULL DEFAULT 0,
            notifications_disabled INTEGER NOT NULL DEFAULT 0,
            missing_scans INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')


def _text(kind, day, at_time, master='', service=''):
    when = f'{day[8:10]}.{day[5:7]}.{day[:4]} в {at_time}'
    prefix = 'NEFOR SPOT. '
    details = f'\nМастер: {master}. Услуга: {service}.' if master or service else ''
    return {
        'created': prefix + f'Вы записаны на {when}.{details}\nЖдём вас!',
        'changed': prefix + f'Ваша запись изменена: новое время {when}.{details} Проверьте детали.',
        'cancelled': prefix + f'Запись на {when} отменена. Если это ошибка, свяжитесь с администратором.',
        'r24': prefix + f'Напоминаем: завтра ваш визит {when}.{details}',
        'r3': prefix + f'Напоминаем: ваш визит сегодня {when}.{details}',
        'review': prefix + 'Спасибо, что были у нас! Будем благодарны за отзыв о NEFOR SPOT.',
    }[kind]


def enqueue(visit_id, kind, day, at_time, phone='', telegram_id=None, master='', service='', enabled=True):
    if not enabled:
        return False
    initialize()
    if kind not in {'created', 'changed', 'cancelled', 'r24', 'r3', 'review'}:
        raise ValueError('event')
    phone = norm_phone(phone)
    uid = int(telegram_id) if telegram_id else None
    suffix = f'{day}:{at_time}' if kind in ('changed', 'r24', 'r3', 'cancelled') else ''
    key = hashlib.sha256(f'{visit_id}|{kind}|{suffix}'.encode()).hexdigest()
    now = int(time.time())
    with db(NOTIFY_DB) as con:
        cur = con.execute('''INSERT OR IGNORE INTO messages(key,visit_id,kind,day,at_time,phone,
            telegram_id,text,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)''',
            (key, str(visit_id), kind, day, at_time, phone, uid,
             _text(kind, day, at_time, master, service), now, now))
        return cur.rowcount > 0


def entries(limit=60):
    initialize()
    with db(NOTIFY_DB) as con:
        return [dict(r) for r in con.execute('''SELECT key,visit_id,kind,day,at_time,phone,telegram_id,
            state,channel,detail,provider_id,created_at,updated_at,text FROM messages
            ORDER BY created_at DESC LIMIT ?''', (max(1, min(int(limit), 200)),))]


def _mark(key, state, channel='', detail='', provider_id=''):
    with db(NOTIFY_DB) as con:
        con.execute('''UPDATE messages SET state=?,channel=?,detail=?,provider_id=?,updated_at=?
            WHERE key=?''', (state, channel, str(detail)[:300], str(provider_id)[:80], int(time.time()), key))


async def _deliver_one(row, *, account_send, bot_send, config):
    key = row['key']
    phone = norm_phone(row['phone'])
    uid = row['telegram_id'] or linked_telegram_id(phone)
    _mark(key, 'attempting')
    if phone and config.get('api_id') and config.get('api_hash'):
        try:
            result = await account_send(phone, row['text'])
        except Exception as exc:
            detail = str(exc).lower()
            unavailable = ('не авториз', 'userprivacyrestricted', 'privacy restricted',
                           'authkeyunregistered', 'no module named', 'could not find input entity')
            if not any(t in detail for t in unavailable):
                _mark(key, 'needs_review', 'telegram_account', 'Outcome uncertain: ' + type(exc).__name__)
                return 'needs_review'
            result = 'not_found'
        if result == 'sent':
            _mark(key, 'sent', 'telegram_account')
            return 'sent'
        if result != 'not_found':
            _mark(key, 'needs_review', 'telegram_account', 'Unknown result')
            return 'needs_review'
    if uid and config.get('client_bot_token'):
        try:
            await bot_send(int(uid), row['text'])
        except Exception as exc:
            msg = str(exc).lower()
            decisive = ('forbidden', 'blocked', 'chat not found', "bot can't initiate", 'user is deactivated')
            if not any(x in msg for x in decisive):
                _mark(key, 'needs_review', 'telegram_bot', 'Outcome uncertain: ' + type(exc).__name__)
                return 'needs_review'
        else:
            _mark(key, 'sent', 'telegram_bot')
            return 'sent'
    detail = 'Отправить SMS вручную с телефона' if phone else 'Нет телефона: уточнить контакт вручную'
    _mark(key, 'manual', 'manual_sms', detail)
    return 'manual'


async def dispatch_pending(*, account_send, bot_send, config, max_count=3):
    initialize()
    with db(NOTIFY_DB) as con:
        pending = [dict(r) for r in con.execute(
            "SELECT * FROM messages WHERE state='queued' ORDER BY created_at LIMIT ?", (max_count,))]
    for row in pending:
        await _deliver_one(row, account_send=account_send, bot_send=bot_send, config=config)
        await asyncio.sleep(1)


def delivery_for_visits(visits):
    initialize()
    visits = list(visits)
    if not visits:
        return {}
    ids = {str(v.get('visit_id') or v.get('id') or '') for v in visits}
    with db(NOTIFY_DB) as con:
        records = [dict(x) for x in con.execute('''SELECT visit_id,day,at_time,kind,state,channel,detail,
            created_at,updated_at FROM messages WHERE kind IN ('created','changed','r24','r3')
            ORDER BY updated_at DESC, created_at DESC''') if x['visit_id'] in ids]
    best = {}
    rank = {'r3': 4, 'r24': 3, 'changed': 2, 'created': 1}
    for record in records:
        if not any(str(v.get('visit_id') or v.get('id')) == record['visit_id']
                   and str(v.get('day')) == record['day']
                   and str(v.get('at_time') or v.get('time')) == record['at_time'] for v in visits):
            continue
        key = (record['visit_id'], record['day'])
        if key not in best or (rank[record['kind']], record['created_at']) > \
                (rank[best[key]['kind']], best[key]['created_at']):
            best[key] = record
    return best


def close_manual_tasks(visit_id, day, state='manual_sms_sent'):
    assert state in ('manual_sms_sent', 'manual_confirmed', 'manual_skipped', 'queued')
    initialize()
    with db(NOTIFY_DB) as con:
        con.execute('''UPDATE messages SET state=?,channel='manual_sms',updated_at=?
            WHERE visit_id=? AND day=? AND state IN ('manual','needs_review')''',
            (state, int(time.time()), str(visit_id), day))


def manual_count() -> int:
    initialize()
    with db(NOTIFY_DB) as con:
        return con.execute("SELECT COUNT(*) FROM messages WHERE state IN ('manual','needs_review')").fetchone()[0]