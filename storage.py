"""SQLite: профили, тикеты, ledger, refs. Единое хранилище."""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import re, secrets, sqlite3, time
from pathlib import Path
from threading import RLock

from config import DATA_DIR

MSK = timezone(timedelta(hours=3))
_LOCK = RLock()

SUPPORT_DB = DATA_DIR / 'nefor_support.db'
NOTIFY_DB = DATA_DIR / 'nefor_notifications.db'
CHATPUSH_DB = DATA_DIR / 'chatpush_dispatch.db'
BOOKING_DB = DATA_DIR / 'booking_ledger.db'
ADMIN_DB = DATA_DIR / 'nefor_spot.db'


def _now() -> str:
    return datetime.now(MSK).isoformat(timespec='seconds')


@contextmanager
def db(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA busy_timeout=20000')
    con.execute('PRAGMA journal_mode=WAL')
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def norm_phone(value) -> str:
    s = ''.join(c for c in str(value or '') if c.isdigit())
    if len(s) == 11 and s.startswith('8'):
        s = '7' + s[1:]
    if len(s) == 10:
        s = '7' + s
    return s if re.fullmatch(r'7\d{10}', s) else ''


# ─── Инициализация всех баз ─────────────────────────────────────────────────

def initialize() -> None:
    with db(SUPPORT_DB) as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS profiles (
            telegram_id INTEGER PRIMARY KEY, username TEXT NOT NULL DEFAULT '',
            full_name TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '',
            phone_verified INTEGER NOT NULL DEFAULT 0,
            notifications_opt_in INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions (
            telegram_id INTEGER PRIMARY KEY, mode TEXT NOT NULL DEFAULT '',
            master TEXT NOT NULL DEFAULT '', service TEXT NOT NULL DEFAULT '',
            pref_day TEXT NOT NULL DEFAULT '', pref_time TEXT NOT NULL DEFAULT '',
            active_ticket INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS tickets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
            content TEXT NOT NULL, phone TEXT NOT NULL DEFAULT '',
            master TEXT NOT NULL DEFAULT '', service TEXT NOT NULL DEFAULT '',
            pref_day TEXT NOT NULL DEFAULT '', pref_time TEXT NOT NULL DEFAULT '',
            media_type TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'new',
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS tickets_status_idx ON tickets(status,created_at);
        CREATE TABLE IF NOT EXISTS ticket_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL,
            who TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS phone_challenges (
            telegram_id INTEGER PRIMARY KEY,
            requested_at INTEGER NOT NULL, expires_at INTEGER NOT NULL);
        ''')

    with db(BOOKING_DB) as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS attempts (
            telegram_id INTEGER NOT NULL, request_key TEXT NOT NULL,
            created_at INTEGER NOT NULL, status TEXT NOT NULL,
            master_id INTEGER NOT NULL, service_id INTEGER NOT NULL,
            appointment_date TEXT NOT NULL, appointment_time TEXT NOT NULL,
            master_name TEXT NOT NULL DEFAULT '', service_name TEXT NOT NULL DEFAULT '',
            visit_id INTEGER, address_id INTEGER, owner_phone TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(telegram_id,request_key));
        CREATE INDEX IF NOT EXISTS idx_attempts_user ON attempts(telegram_id,created_at);
        CREATE TABLE IF NOT EXISTS client_visit_refs (
            ref TEXT PRIMARY KEY, telegram_id INTEGER NOT NULL,
            visit_id INTEGER NOT NULL, revision TEXT NOT NULL, expires_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS visit_actions (
            telegram_id INTEGER NOT NULL, request_key TEXT NOT NULL,
            visit_id INTEGER NOT NULL, action TEXT NOT NULL,
            target_date TEXT NOT NULL, target_time TEXT NOT NULL,
            state TEXT NOT NULL, created_at INTEGER NOT NULL,
            result TEXT NOT NULL DEFAULT '', PRIMARY KEY(telegram_id,request_key));
        CREATE INDEX IF NOT EXISTS actions_visit ON visit_actions(visit_id,state);
        ''')

    with db(ADMIN_DB) as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS visits (
            visit_id TEXT NOT NULL, day TEXT NOT NULL, at_time TEXT NOT NULL,
            name TEXT NOT NULL, phone TEXT NOT NULL, master TEXT NOT NULL,
            service TEXT NOT NULL, notifications_disabled INTEGER NOT NULL DEFAULT 0,
            manual_state TEXT NOT NULL DEFAULT 'pending', snapshot_at TEXT NOT NULL,
            PRIMARY KEY(visit_id,day));
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS calendar_snapshots (day TEXT PRIMARY KEY, fetched_at TEXT NOT NULL);
        ''')
        con.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('auto_enabled','0')")


# ─── Профили ────────────────────────────────────────────────────────────────

def upsert_profile(uid: int, username: str, full_name: str) -> None:
    with db(SUPPORT_DB) as con:
        con.execute('''INSERT INTO profiles(telegram_id,username,full_name,updated_at)
            VALUES (?,?,?,?) ON CONFLICT(telegram_id) DO UPDATE SET
            username=excluded.username, full_name=excluded.full_name,
            updated_at=excluded.updated_at''',
            (int(uid), str(username or '')[:80], str(full_name or '')[:150], _now()))


def profile(uid: int):
    with db(SUPPORT_DB) as con:
        row = con.execute('SELECT * FROM profiles WHERE telegram_id=?', (int(uid),)).fetchone()
        return dict(row) if row else None


def public_profile(uid: int) -> dict:
    row = profile(uid) or {}
    return {
        'phone_verified': bool(row.get('phone_verified')),
        'notifications_opt_in': bool(row.get('notifications_opt_in')),
        'phone': ('+' + row['phone']) if row.get('phone_verified') and row.get('phone') else '',
    }


def begin_phone_verification(uid: int) -> None:
    at = int(time.time())
    with db(SUPPORT_DB) as con:
        con.execute('''INSERT INTO phone_challenges VALUES (?,?,?)
            ON CONFLICT(telegram_id) DO UPDATE SET
            requested_at=excluded.requested_at, expires_at=excluded.expires_at''',
            (int(uid), at, at + 180))


def accept_contact(uid: int, contact: dict, message_date: int) -> bool:
    if int(contact.get('user_id') or 0) != int(uid):
        return False
    phone = norm_phone(contact.get('phone_number'))
    if not phone:
        return False
    with db(SUPPORT_DB) as con:
        con.execute('BEGIN IMMEDIATE')
        ch = con.execute('SELECT * FROM phone_challenges WHERE telegram_id=?', (int(uid),)).fetchone()
        if not ch or ch['expires_at'] < time.time() or int(message_date or 0) < ch['requested_at'] - 2:
            return False
        con.execute('UPDATE profiles SET phone=?,phone_verified=1,updated_at=? WHERE telegram_id=?',
                    (phone, _now(), int(uid)))
        con.execute('DELETE FROM phone_challenges WHERE telegram_id=?', (int(uid),))
    return True


def linked_telegram_id(phone: str):
    phone = norm_phone(phone)
    if not phone:
        return None
    with db(SUPPORT_DB) as con:
        rows = con.execute('SELECT telegram_id, phone FROM profiles WHERE phone_verified=1 AND notifications_opt_in=1').fetchall()
    ids = [int(r['telegram_id']) for r in rows if norm_phone(r['phone']) == phone]
    return ids[0] if len(ids) == 1 else None


# ─── Сессии ─────────────────────────────────────────────────────────────────

def session(uid: int) -> dict:
    with db(SUPPORT_DB) as con:
        row = con.execute('SELECT * FROM sessions WHERE telegram_id=?', (int(uid),)).fetchone()
    if row:
        return dict(row)
    return {'telegram_id': int(uid), 'mode': '', 'master': '', 'service': '',
            'pref_day': '', 'pref_time': '', 'active_ticket': 0}


def set_session(uid: int, **fields) -> None:
    allowed = {'mode', 'master', 'service', 'pref_day', 'pref_time', 'active_ticket'}
    if any(k not in allowed for k in fields):
        raise ValueError('Invalid session field')
    with db(SUPPORT_DB) as con:
        con.execute('INSERT OR IGNORE INTO sessions(telegram_id) VALUES (?)', (int(uid),))
        for k, v in fields.items():
            con.execute(f'UPDATE sessions SET {k}=? WHERE telegram_id=?', (str(v)[:180], int(uid)))


def reset_session(uid: int) -> None:
    set_session(uid, mode='', master='', service='', pref_day='', pref_time='', active_ticket='0')


# ─── Тикеты ─────────────────────────────────────────────────────────────────

def create_ticket(uid: int, kind: str, content: str, **extra) -> int:
    person = profile(uid) or {}
    created = _now()
    with db(SUPPORT_DB) as con:
        cur = con.execute('''INSERT INTO tickets(telegram_id,name,kind,content,phone,master,service,
            pref_day,pref_time,media_type,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
            (int(uid), str(person.get('full_name') or f'Клиент {uid}')[:150], str(kind)[:40],
             str(content)[:1500], str(person.get('phone') or ''),
             str(extra.get('master', ''))[:150], str(extra.get('service', ''))[:150],
             str(extra.get('pref_day', ''))[:20], str(extra.get('pref_time', ''))[:150],
             str(extra.get('media_type', ''))[:40], created, created))
        tid = cur.lastrowid
        con.execute('INSERT INTO ticket_messages(ticket_id,who,text,created_at) VALUES (?,?,?,?)',
                    (tid, 'client', str(content)[:1500], created))
    return tid


def add_ticket_message(ticket_id: int, who: str, text: str) -> None:
    if who not in ('client', 'admin'):
        raise ValueError('Bad sender')
    ts = _now()
    with db(SUPPORT_DB) as con:
        con.execute('INSERT INTO ticket_messages(ticket_id,who,text,created_at) VALUES (?,?,?,?)',
                    (int(ticket_id), who, str(text)[:1500], ts))
        if who == 'client':
            con.execute("UPDATE tickets SET content=substr(content||'\n'||?,1,2500),status='new',updated_at=? WHERE id=?",
                        (str(text)[:700], ts, int(ticket_id)))
        else:
            con.execute('UPDATE tickets SET updated_at=? WHERE id=?', (ts, int(ticket_id)))


def ticket(ticket_id: int):
    with db(SUPPORT_DB) as con:
        row = con.execute('SELECT * FROM tickets WHERE id=?', (int(ticket_id),)).fetchone()
        return dict(row) if row else None


def tickets(status=None, limit: int = 15):
    with db(SUPPORT_DB) as con:
        if status:
            rows = con.execute('SELECT * FROM tickets WHERE status=? ORDER BY id DESC LIMIT ?',
                               (status, limit)).fetchall()
        else:
            rows = con.execute('SELECT * FROM tickets ORDER BY id DESC LIMIT ?', (limit,)).fetchall()
    return [dict(r) for r in rows]


def own_tickets(uid: int, limit: int = 30):
    with db(SUPPORT_DB) as con:
        rows = con.execute('''SELECT id,kind,status,content,created_at,updated_at
            FROM tickets WHERE telegram_id=? ORDER BY id DESC LIMIT ?''',
            (int(uid), min(max(int(limit), 1), 50))).fetchall()
    return [dict(r) for r in rows]


def set_ticket_status(ticket_id: int, status: str) -> None:
    if status not in ('new', 'answered', 'closed'):
        raise ValueError('Invalid ticket state')
    with db(SUPPORT_DB) as con:
        con.execute('UPDATE tickets SET status=?,updated_at=? WHERE id=?',
                    (status, _now(), int(ticket_id)))


def conversation(tid: int):
    with db(SUPPORT_DB) as con:
        rows = con.execute('''SELECT who,text,created_at FROM ticket_messages
            WHERE ticket_id=? ORDER BY id ASC LIMIT 200''', (int(tid),)).fetchall()
    return [dict(r) for r in rows]


# ─── Booking ledger ─────────────────────────────────────────────────────────

def ledger_start(uid: int, key: str, payload: dict, master_name='', service_name=''):
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,100}', key):
        raise ValueError('request_key')
    with _LOCK, db(BOOKING_DB) as con:
        con.execute('BEGIN IMMEDIATE')
        prev = con.execute('SELECT * FROM attempts WHERE telegram_id=? AND request_key=?', (uid, key)).fetchone()
        if prev:
            if (prev['master_id'], prev['service_id'], prev['appointment_date'], prev['appointment_time']) != \
               (payload['masterID'], payload['serviceID'], payload['date'], payload['timeFrom']):
                raise ValueError('request_key used')
            return _existing(prev)
        now = int(time.time())
        similar = con.execute('''SELECT * FROM attempts WHERE telegram_id=? AND master_id=? AND service_id=?
            AND appointment_date=? AND appointment_time=? AND created_at>?
            AND status IN ('pending','done','uncertain')
            ORDER BY created_at DESC LIMIT 1''',
            (uid, payload['masterID'], payload['serviceID'], payload['date'],
             payload['timeFrom'], now - 86400)).fetchone()
        if similar:
            return _existing(similar)
        cnt = con.execute('''SELECT COUNT(*) FROM attempts WHERE telegram_id=? AND created_at>?
            AND status IN ('pending','done','uncertain')''', (uid, now - 900)).fetchone()[0]
        if cnt >= 3:
            raise ValueError('rate_limited')
        con.execute('''INSERT INTO attempts(telegram_id,request_key,created_at,status,master_id,service_id,
            appointment_date,appointment_time,master_name,service_name,owner_phone)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
            (uid, key, now, 'pending', payload['masterID'], payload['serviceID'],
             payload['date'], payload['timeFrom'], master_name, service_name,
             payload.get('phoneClient', '')))
        return None


def _existing(row) -> dict:
    if row['status'] == 'done':
        return {'visitID': row['visit_id'], 'addressID': row['address_id']}
    if row['status'] == 'rejected':
        raise ValueError('rejected')
    raise ValueError('uncertain')


def ledger_complete(uid: int, key: str, status: str, result=None) -> None:
    with _LOCK, db(BOOKING_DB) as con:
        con.execute('''UPDATE attempts SET status=?,visit_id=?,address_id=?
            WHERE telegram_id=? AND request_key=? AND status='pending' ''',
            (status, result.get('visitID') if result else None,
             result.get('addressID') if result else None, uid, key))


def ledger_recent(uid: int):
    with _LOCK, db(BOOKING_DB) as con:
        rows = con.execute('''SELECT visit_id,appointment_date,appointment_time,master_name,service_name,status
            FROM attempts WHERE telegram_id=? AND status='done'
            ORDER BY created_at DESC LIMIT 20''', (uid,)).fetchall()
    return [{'visitID': r['visit_id'], 'date': r['appointment_date'], 'time': r['appointment_time'],
             'master': r['master_name'], 'service': r['service_name'], 'source': 'miniapp'} for r in rows]


def ledger_admin_recent(limit: int = 50):
    with _LOCK, db(BOOKING_DB) as con:
        rows = con.execute('''SELECT telegram_id,created_at,status,master_name,service_name,
            appointment_date,appointment_time,visit_id FROM attempts
            ORDER BY created_at DESC LIMIT ?''', (limit,)).fetchall()
    return [dict(r) for r in rows]


def ledger_owns(uid: int, visit_id: int, crm_phone: str) -> bool:
    with db(BOOKING_DB) as con:
        row = con.execute("SELECT owner_phone FROM attempts WHERE telegram_id=? AND visit_id=? AND status='done'",
                          (uid, int(visit_id))).fetchone()
    return bool(row and (not row['owner_phone'] or norm_phone(row['owner_phone']) == norm_phone(crm_phone)))


def ledger_sync_visit(visit_id: int, day: str, at_time: str) -> None:
    with db(BOOKING_DB) as con:
        con.execute("UPDATE attempts SET appointment_date=?,appointment_time=? WHERE visit_id=? AND status='done'",
                    (day, at_time, int(visit_id)))


# ─── Visit refs ─────────────────────────────────────────────────────────────

def ref_create(uid: int, visit_id: int, revision: str, ttl: int = 3600) -> str:
    ref = secrets.token_urlsafe(24)
    with db(BOOKING_DB) as con:
        con.execute('DELETE FROM client_visit_refs WHERE expires_at<?', (int(time.time()),))
        con.execute('INSERT INTO client_visit_refs VALUES (?,?,?,?,?)',
                    (ref, uid, int(visit_id), revision, int(time.time()) + ttl))
    return ref


def ref_get(uid: int, ref: str):
    if not isinstance(ref, str):
        return None
    with db(BOOKING_DB) as con:
        row = con.execute('''SELECT * FROM client_visit_refs
            WHERE ref=? AND telegram_id=? AND expires_at>?''',
            (ref, uid, int(time.time()))).fetchone()
    return dict(row) if row else None


def action_get(uid: int, key: str):
    with db(BOOKING_DB) as con:
        row = con.execute('SELECT * FROM visit_actions WHERE telegram_id=? AND request_key=?',
                          (uid, key)).fetchone()
    return dict(row) if row else None


def action_active_for_visit(visit_id: int):
    with db(BOOKING_DB) as con:
        row = con.execute('''SELECT * FROM visit_actions WHERE visit_id=?
            AND state IN ('pending','uncertain') LIMIT 1''', (int(visit_id),)).fetchone()
    return dict(row) if row else None


def action_insert(uid: int, key: str, visit_id: int, action: str, day: str, at_time: str, body: str) -> None:
    with db(BOOKING_DB) as con:
        con.execute('''INSERT INTO visit_actions(telegram_id,request_key,visit_id,action,
            target_date,target_time,state,created_at,result) VALUES (?,?,?,?,?,?,?,?,?)''',
            (uid, key, int(visit_id), action, day, at_time, 'pending',
             int(time.time()), body))


def action_update_state(uid: int, key: str, state: str) -> None:
    with db(BOOKING_DB) as con:
        con.execute("UPDATE visit_actions SET state=? WHERE telegram_id=? AND request_key=? AND state='pending'",
                    (state, uid, key))


def action_pending_for(uid: int):
    with db(BOOKING_DB) as con:
        rows = con.execute('''SELECT * FROM visit_actions WHERE telegram_id=?
            AND state IN ('pending','uncertain') ORDER BY created_at DESC LIMIT 20''', (uid,)).fetchall()
    return [dict(r) for r in rows]


# ─── Настройки админа ──────────────────────────────────────────────────────

def get_setting(key: str, default: str = '') -> str:
    with db(ADMIN_DB) as con:
        row = con.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row['value'] if row else default


def set_setting(key: str, value) -> None:
    with db(ADMIN_DB) as con:
        con.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                    (key, str(value)))


def save_visits(day: str, items: list) -> None:
    now = datetime.now(MSK).isoformat()
    with db(ADMIN_DB) as con:
        for i in items:
            if not i['id']:
                continue
            con.execute('''INSERT INTO visits(visit_id,day,at_time,name,phone,master,service,
                notifications_disabled,snapshot_at) VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(visit_id,day) DO UPDATE SET
                at_time=excluded.at_time,name=excluded.name,phone=excluded.phone,
                master=excluded.master,service=excluded.service,
                notifications_disabled=excluded.notifications_disabled,snapshot_at=excluded.snapshot_at''',
                (i['id'], day, i['time'], i['name'], i['phone'], i['master'], i['service'],
                 int(i.get('notifications_disabled', False)), now))


def rows_for_date(day: str):
    with db(ADMIN_DB) as con:
        return [dict(r) for r in con.execute('SELECT * FROM visits WHERE day=? ORDER BY at_time,name', (day,))]


def find_visit(visit_id, day=None):
    if day is None:
        day = (datetime.now(MSK) + timedelta(days=1)).date().isoformat()
    with db(ADMIN_DB) as con:
        r = con.execute('SELECT * FROM visits WHERE visit_id=? AND day=?', (str(visit_id), day)).fetchone()
        return dict(r) if r else None


def update_manual_state(visit_id, day, state) -> bool:
    with db(ADMIN_DB) as con:
        return con.execute('UPDATE visits SET manual_state=? WHERE visit_id=? AND day=?',
                           (state, visit_id, day)).rowcount > 0