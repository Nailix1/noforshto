"""ChatPush: API-доставка + read-only web-история."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib, hmac, html, json, os, re, threading, time
from urllib import parse, request, error

from storage import db as _db, norm_phone, CHATPUSH_DB

MSK = timezone(timedelta(hours=3))
API_ACCOUNT = 'https://api.pushsms.ru/api/v1/account'
API_DELIVERY = 'https://api.pushsms.ru/api/v1/delivery'
SUCCESS = {2, 16}
ERROR_CODES = {0, 3, 4, 5, 7, 8, 9, 10, 11, 12, 13, 17, 22, 25, 26, 28, 31,
               32, 33, 34, 35, 44, 45, 50, 555}

_EXTERNAL_BASE = 'https://chatpush.ru/front_office/deliveries'
_EXT_LOCK = threading.RLock()
_EXT_CACHE = {}
_EXT_LAST = {'connected': False, 'checked_at': 0, 'reason': 'not_configured'}
_EXT_MAX_AGE = timedelta(days=90)
_EXT_TTL = 180
_EXT_ERRORS = ('tdlib_user_not_found', 'tdlib_send_to_contact_only_error',
               'пользователь не найден в telegram', 'нет чата в telegram для отправки')
_EXT_OK = ('доставлено', 'прочитано', 'delivered', 'read')
_EXT_PENDING = ('принято', 'ожидает отправки', 'в обработке', 'in_progress',
                'waiting_for_sending', 'accepted', 'scheduled')


def token() -> str:
    return os.environ.get('NEFOR_CHATPUSH_TOKEN', '').strip()


def configured() -> bool:
    return bool(token())


def initialize() -> None:
    with _db(CHATPUSH_DB) as con:
        con.executescript('''
        CREATE TABLE IF NOT EXISTS deliveries (
            attempt_key TEXT PRIMARY KEY, visit_id TEXT NOT NULL, day TEXT NOT NULL,
            at_time TEXT NOT NULL, phone TEXT NOT NULL, purpose TEXT NOT NULL,
            state TEXT NOT NULL, provider_id TEXT NOT NULL DEFAULT '',
            external_id TEXT NOT NULL, provider_status INTEGER,
            route TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL, UNIQUE(visit_id,day,at_time,purpose));
        CREATE TABLE IF NOT EXISTS external_audit (
            visit_id TEXT NOT NULL, day TEXT NOT NULL, at_time TEXT NOT NULL,
            state TEXT NOT NULL, checked_at INTEGER NOT NULL,
            PRIMARY KEY(visit_id,day,at_time));
        ''')


def _key(visit_id, day, at_time, purpose='r24') -> str:
    payload = f'{visit_id}|{day}|{at_time}|{purpose}'
    return hashlib.sha256(('nefor-chatpush-v1|' + payload).encode()).hexdigest()[:40]


def _signature(key: str) -> str:
    return hmac.new(token().encode(), ('nefor-status-callback|' + key).encode(), hashlib.sha256).hexdigest()


def _request(method: str, url: str, payload=None, timeout=12):
    secret = token()
    if not secret:
        return 0, {}
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = request.Request(url, data=data, method=method, headers={
        'Authorization': 'Bearer ' + secret, 'Accept': 'application/json',
        'Content-Type': 'application/json'})
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(32768)
            return resp.status, json.loads(body.decode('utf-8')) if body else {}
    except error.HTTPError as exc:
        exc.close()
        return exc.code, {}
    except (OSError, UnicodeDecodeError, ValueError):
        return 0, {}


def auth_status():
    if not configured():
        return {'configured': False, 'available': False, 'api_http_status': 0}
    try:
        code, _ = _request('GET', API_ACCOUNT)
    except Exception:
        code = 0
    return {'configured': True, 'available': code == 200, 'api_http_status': int(code)}


def current(visit_id, day, at_time=''):
    initialize()
    with _db(CHATPUSH_DB) as con:
        if at_time:
            row = con.execute('''SELECT * FROM deliveries WHERE visit_id=? AND day=? AND at_time=?
                ORDER BY updated_at DESC LIMIT 1''',
                (str(visit_id), str(day), str(at_time))).fetchone()
        else:
            row = con.execute('''SELECT * FROM deliveries WHERE visit_id=? AND day=?
                ORDER BY updated_at DESC LIMIT 1''', (str(visit_id), str(day))).fetchone()
    return dict(row) if row else None


def public_state(row: dict) -> dict:
    state = row['state']
    label = {
        'attempting': 'Передача в ChatPush — результат ещё неизвестен',
        'accepted': 'ChatPush принял в очередь',
        'delivered': 'Telegram доставлено · ChatPush',
        'read': 'Telegram прочитано · ChatPush',
        'failed': 'ChatPush не доставил — проверить / SMS',
        'rejected': 'ChatPush отклонил отправку — проверить',
        'uncertain': 'Результат ChatPush не определён — проверить вручную',
    }.get(state, 'ChatPush: требуется проверка')
    return {'state': state, 'label': label, 'channel': row['route'] or 'tdlib',
            'provider_status': row['provider_status'], 'provider_id': row['provider_id']}


def for_visits(visits):
    initialize()
    result = {}
    for v in visits:
        row = current(v.get('visit_id'), v.get('day'), v.get('at_time'))
        if row:
            result[(str(v['visit_id']), str(v['day']))] = public_state(row)
    return result


def apply_provider_result(key: str, state: str, provider_id='', provider_status=None, route=''):
    if state not in ('attempting', 'accepted', 'delivered', 'read', 'failed', 'rejected', 'uncertain'):
        raise ValueError('Недопустимый статус доставки')
    initialize()
    with _db(CHATPUSH_DB) as con:
        con.execute('BEGIN IMMEDIATE')
        old = con.execute('SELECT * FROM deliveries WHERE attempt_key=?', (str(key),)).fetchone()
        if not old:
            return False
        if old['state'] in ('delivered', 'read') and state not in ('delivered', 'read'):
            return True
        if old['provider_id'] and provider_id and str(old['provider_id']) != str(provider_id):
            return False
        con.execute('''UPDATE deliveries SET state=?,provider_id=?,provider_status=?,route=?,updated_at=?
            WHERE attempt_key=?''', (state, str(provider_id or old['provider_id']),
             provider_status if provider_status is not None else old['provider_status'],
             str(route or old['route']), int(time.time()), str(key)))
    return True


def callback(raw_query: str, payload: dict) -> bool:
    q = parse.parse_qs(raw_query)
    key = q.get('key', [''])[0]
    sig = q.get('sig', [''])[0]
    if not re.fullmatch('[a-f0-9]{40}', key) or not re.fullmatch('[a-f0-9]{64}', sig) or not token():
        return False
    if not hmac.compare_digest(sig, _signature(key)):
        return False
    if not isinstance(payload, dict) or not isinstance(payload.get('delivery'), dict):
        return False
    record = payload['delivery']
    provider_id = str(record.get('id') or '')
    if not re.fullmatch('[0-9]{1,24}', provider_id):
        return False
    details = record.get('status')
    if not isinstance(details, dict):
        return False
    try:
        code = int(details['status_id'])
    except (ValueError, TypeError, KeyError):
        return False
    routing = str(details.get('dispatch_routing') or 'tdlib')
    if routing not in ('tdlib', 'telegram', 'sms', 'whatsapp', 'notify'):
        return False
    state = 'read' if code == 16 else 'delivered' if code == 2 else 'failed' if code in ERROR_CODES else 'accepted'
    return apply_provider_result(key, state, provider_id, code, routing)


def reconcile_external(visit_id, day, at_time, state):
    if state not in ('delivered', 'failed', 'unknown'):
        raise ValueError('Unknown external delivery state')
    initialize()
    with _db(CHATPUSH_DB) as con:
        con.execute('''INSERT INTO external_audit(visit_id,day,at_time,state,checked_at)
            VALUES(?,?,?,?,?) ON CONFLICT(visit_id,day,at_time) DO UPDATE SET
            state=excluded.state,checked_at=excluded.checked_at''',
            (str(visit_id), str(day), str(at_time), state, int(time.time())))


def external_for_visits(visits):
    initialize()
    result = {}
    with _db(CHATPUSH_DB) as con:
        for v in visits:
            vid = str(v.get('visit_id') or '')
            day = str(v.get('day') or '')
            slot = str(v.get('at_time') or '')
            row = con.execute('SELECT state,checked_at FROM external_audit WHERE visit_id=? AND day=? AND at_time=?',
                              (vid, day, slot)).fetchone()
            if row:
                result[(vid, day, slot)] = {'state': row['state'], 'checked_at': row['checked_at']}
    return result


def history_enabled() -> bool:
    return bool(os.environ.get('NEFOR_CHATPUSH_WEB_COOKIE', '').strip())


def history_connection_status():
    if not history_enabled():
        return {'configured': False, 'connected': False, 'checked_at': 0, 'reason': 'not_configured'}
    with _EXT_LOCK:
        return {'configured': True, 'connected': bool(_EXT_LAST['connected']),
                'checked_at': _EXT_LAST['checked_at'], 'reason': _EXT_LAST['reason']}


def _plain(raw: str) -> str:
    raw = re.sub(r'(?is)<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>', ' ', raw)
    raw = re.sub(r'(?is)<[^>]+>', ' ', raw)
    return ' '.join(html.unescape(raw).replace('\xa0', ' ').split())


def _recent(text: str, *, now=None) -> bool:
    now = now or datetime.now(MSK)
    m = re.search(r'(?<!\d)(\d{2})\.(\d{2})\.(20\d{2})(?!\d)', text)
    if m:
        d, mo, y = map(int, m.groups())
        try:
            dt = datetime(y, mo, d, tzinfo=MSK)
        except ValueError:
            return False
    else:
        m = re.search(r'(?<!\d)(20\d{2})-(\d{2})-(\d{2})(?!\d)', text)
        if not m:
            return False
        y, mo, d = map(int, m.groups())
        try:
            dt = datetime(y, mo, d, tzinfo=MSK)
        except ValueError:
            return False
    return now - _EXT_MAX_AGE <= dt <= now + timedelta(days=2)


def _classify_html(source: str, *, now=None) -> str:
    if not isinstance(source, str) or 'история сообщений' not in source.lower():
        return 'auth_required'
    match = None
    for table in re.findall(r'(?is)<table\b[^>]*>(.*?)</table>', source):
        head = re.search(r'(?is)<thead\b[^>]*>(.*?)</thead>', table)
        labels = _plain(head.group(1)).lower() if head else ''
        if 'телефон' in labels and 'статус' in labels:
            match = re.search(r'(?is)<tbody\b[^>]*>(.*?)</tbody>', table)
            if match:
                break
    if match is None:
        match = re.search(r'(?is)<tbody\b[^>]*>(.*?)</tbody>', source)
    if not match:
        return 'unknown'
    rows = re.findall(r'(?is)<tr\b[^>]*>(.*?)</tr>', match.group(1))
    for row in rows:
        plain = _plain(row).lower()
        raw = (plain + ' ' + html.unescape(row).lower())
        if not any(s in raw for s in ('tdlib', 'telegram', 'телеграм')):
            continue
        if not (_recent(plain, now=now) or _recent(raw, now=now)):
            continue
        if any(err in raw for err in _EXT_ERRORS):
            return 'no_telegram'
        if any(phrase in raw for phrase in ('не доставлено', 'undeliverable', 'ошибка', 'отклонено')):
            return 'unknown'
        if any(ok in raw for ok in _EXT_OK):
            return 'telegram'
        if any(p in raw for p in _EXT_PENDING):
            return 'pending'
        return 'unknown'
    return 'unknown'


def _read_one(phone: str, cookie: str) -> str:
    params = parse.urlencode({
        'per_page': '100', 'q[phone_or_tdlib_user_id_str_cont]': phone,
        'q[provider_delivery_infos_dispatch_type_in][]': 'tdlib', 'q[s]': 'executed_at desc'})
    req = request.Request(_EXTERNAL_BASE + '?' + params, headers={
        'Cookie': cookie, 'Accept': 'text/html',
        'User-Agent': 'Mozilla/5.0 (compatible; NEFOR-SPOT-admin-readonly/1.0)',
        'Cache-Control': 'no-cache'}, method='GET')
    try:
        with request.urlopen(req, timeout=10) as response:
            if response.status != 200 or not response.geturl().startswith(_EXTERNAL_BASE):
                return 'auth_required'
            return _classify_html(response.read(750000).decode('utf-8', 'replace'))
    except (OSError, ValueError, error.HTTPError):
        return 'unavailable'


def history_for_visits(visits):
    cookie = os.environ.get('NEFOR_CHATPUSH_WEB_COOKIE', '').strip()
    if not cookie:
        return {}
    fingerprint = hashlib.sha256(cookie.encode('utf-8')).digest()
    with _EXT_LOCK:
        if _EXT_LAST.get('session_fingerprint') != fingerprint:
            _EXT_CACHE.clear()
            _EXT_LAST.update(connected=False, checked_at=0,
                             reason='not_checked', session_fingerprint=fingerprint)
        if _EXT_LAST['reason'] == 'web_auth_required' and time.time() - _EXT_LAST['checked_at'] < _EXT_TTL:
            return {}
    phones = sorted({norm_phone(v.get('phone')) for v in visits} - {''})[:40]
    if not phones:
        return {}
    now = time.time()
    with _EXT_LOCK:
        hits = {p: _EXT_CACHE[p][1] for p in phones if p in _EXT_CACHE and now - _EXT_CACHE[p][0] < _EXT_TTL}
    pending = [p for p in phones if p not in hits]
    if pending:
        with ThreadPoolExecutor(max_workers=4) as pool:
            outcome = list(pool.map(lambda p: _read_one(p, cookie), pending))
        with _EXT_LOCK:
            for p, state in zip(pending, outcome):
                if state != 'unavailable':
                    _EXT_CACHE[p] = (time.time(), state)
                hits[p] = state
            active = any(x not in ('auth_required', 'unavailable') for x in hits.values())
            _EXT_LAST.update(connected=active, checked_at=int(time.time()),
                             reason='ok' if active else (
                                 'web_auth_required' if 'auth_required' in hits.values()
                                 else 'network_unavailable'))
    return {p: {'state': state, 'source': 'chatpush_web_history', 'checked_at': int(now)}
            for p, state in hits.items() if state not in ('auth_required', 'unavailable')}