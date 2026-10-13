"""Tetradka CRM: авторизация, каталог, бронирование."""
from __future__ import annotations
import base64, concurrent.futures, hashlib, hmac, http.cookiejar, json, os, re, secrets, socket, ssl
import threading, time, urllib.error, urllib.parse, urllib.request
from datetime import date, datetime, timedelta, timezone

from config import cfg, DATA_DIR
from storage import norm_phone

BASE_URL = 'https://booking-api.tbookme.ru'
ONLINE_ID = '187686'
CRM_ROOT = 'https://tetradka.io'
MSK = timezone(timedelta(hours=3))
MAX_TIMEOUT = 12

_CATALOG_CACHE = None
_CATALOG_EXPIRES = 0.0
_CATALOG_LOCK = threading.Lock()
_TOKEN = ''
_TOKEN_EXPIRES = 0.0
_AUTH_LOCK = threading.RLock()


class BookingError(Exception):
    def __init__(self, message: str, status_code: int = 400, code: str = 'invalid'):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class CRMAuthError(RuntimeError):
    def __init__(self, message: str, *, code: str = 'unknown'):
        super().__init__(message)
        self.code = code


class ScheduleFetchError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__('CRM calendar unavailable')


# ─── Валидация ──────────────────────────────────────────────────────────────

def positive_id(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise BookingError('Выберите мастера и услугу.')
    if not re.fullmatch(r'[1-9][0-9]*', str(value)):
        raise BookingError('Выберите мастера и услугу.')
    number = int(value)
    if number > 9007199254740991:
        raise BookingError('Некорректный идентификатор.')
    return number


def validate_booking(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise BookingError('Не удалось прочитать данные записи.')
    name = payload.get('nameClient')
    phone = payload.get('phoneClient')
    comment = payload.get('comment', '')
    day = payload.get('date')
    time_from = payload.get('timeFrom')
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 150:
        raise BookingError('Введите имя клиента (до 150 символов).')
    if not isinstance(phone, str) or not 10 <= len(re.sub(r'\D', '', phone)) <= 15 or len(phone) > 50:
        raise BookingError('Введите корректный телефон.')
    if not isinstance(comment, str) or len(comment) > 500:
        raise BookingError('Комментарий слишком длинный.')
    if not isinstance(day, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
        raise BookingError('Неверная дата записи.')
    try:
        date.fromisoformat(day)
    except ValueError as exc:
        raise BookingError('Неверная дата записи.') from exc
    if not isinstance(time_from, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', time_from):
        raise BookingError('Неверное время записи.')
    if payload.get('tetradkaPersonalDataProcessing') is not True:
        raise BookingError('Необходимо согласие на обработку персональных данных.')
    return {'nameClient': name.strip(), 'phoneClient': phone.strip(), 'comment': comment.strip(),
            'date': day, 'timeFrom': time_from,
            'masterID': positive_id(payload.get('masterID')),
            'serviceID': positive_id(payload.get('serviceID'))}


def check_date(day: str) -> date:
    try:
        selected = date.fromisoformat(day)
    except (TypeError, ValueError):
        raise BookingError('Выберите дату записи.')
    today = datetime.now(MSK).date()
    if selected < today or selected > today + timedelta(days=60):
        raise BookingError('Можно выбрать дату в ближайшие 60 дней.')
    return selected


# ─── Низкоуровневый HTTP ────────────────────────────────────────────────────

def _proxy_request(method: str, url: str, data):
    endpoint = os.environ.get('NEFOR_TETRADKA_PROXY_BASE', '').strip().rstrip('/')
    key = os.environ.get('NEFOR_TETRADKA_PROXY_SECRET', '')
    parsed = urllib.parse.urlsplit(url)
    if not endpoint or parsed.hostname != 'booking-api.tbookme.ru':
        return url, {}
    proxy = urllib.parse.urlsplit(endpoint)
    if (proxy.scheme != 'http' or proxy.hostname != 'tetradka-eu-connectivity-check.railway.internal'
            or proxy.port != 8787 or proxy.username or proxy.password or proxy.query
            or proxy.fragment or proxy.path not in ('', '/') or len(key) < 32):
        raise BookingError('Сервис онлайн-записи временно не настроен.', 503, 'proxy_config')
    target = parsed.path + ('?' + parsed.query if parsed.query else '')
    nonce = secrets.token_urlsafe(18)
    ts = str(int(time.time()))
    message = '\n'.join((ts, nonce, method, target, hashlib.sha256(data or b'').hexdigest()))
    signature = hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()
    return endpoint + '/proxy' + target, {
        'X-Nefor-Timestamp': ts, 'X-Nefor-Nonce': nonce, 'X-Nefor-Signature': signature}


def _network_category(exc):
    cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(cause, socket.gaierror): return 'dns'
    if isinstance(cause, ssl.SSLCertVerificationError): return 'tls_certificate'
    if isinstance(cause, ssl.SSLError): return 'tls'
    if isinstance(cause, (TimeoutError, socket.timeout)): return 'timeout'
    if isinstance(cause, ConnectionRefusedError): return 'connection_refused'
    if isinstance(cause, ConnectionResetError): return 'connection_reset'
    return 'network_other'


def _request_json(method: str, url: str, body, timeout: int = MAX_TIMEOUT):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode('utf-8')
    is_read = method == 'GET'
    tries = 2 if is_read else 1
    for attempt in range(tries):
        dest, extra = _proxy_request(method, url, data)
        req = urllib.request.Request(dest, data=data, method=method, headers={
            'Accept': 'application/json', 'Content-Type': 'application/json', **extra})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read(262145)
                if len(raw) > 262144:
                    raise BookingError('Некорректный ответ.', 502, 'upstream')
                try:
                    return resp.status, json.loads(raw.decode('utf-8'))
                except (UnicodeError, ValueError):
                    raise BookingError('Некорректный ответ.', 502, 'upstream')
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            if is_read and attempt + 1 < tries and code in (429, 502, 503, 504):
                time.sleep(.35)
                continue
            return code, None
        except (OSError, TimeoutError) as exc:
            if is_read and attempt + 1 < tries:
                time.sleep(.35)
                continue
            if is_read:
                print('NEFOR_BOOKING_NETWORK_FAILURE category=' + _network_category(exc), flush=True)
                raise BookingError('Не удалось загрузить расписание. Попробуйте обновить.', 503, 'read_unavailable') from exc
            raise BookingError('Ответ Тетрадки неизвестен. Проверьте запись у администратора.', 503, 'uncertain') from exc
    raise BookingError('Тетрадка временно недоступна.', 503, 'read_unavailable')


# ─── CRM auth ───────────────────────────────────────────────────────────────

def crm_configured() -> bool:
    return bool(cfg('tetradka_token')) or bool(
        os.environ.get('NEFOR_TETRADKA_TOKEN') or
        (os.environ.get('NEFOR_TETRADKA_LOGIN') and os.environ.get('NEFOR_TETRADKA_PASSWORD')))


def _cache_path():
    return DATA_DIR / 'tetradka_session.json'


def _load_cached():
    path = _cache_path()
    if not path.exists():
        return None
    try:
        saved = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    token = saved.get('token')
    issued = float(saved.get('saved_at') or path.stat().st_mtime)
    if isinstance(token, str) and token and 0 <= time.time() - issued <= 72 * 3600:
        return token, float(saved.get('expires', 0))
    return None


def _save_cached(token: str, expires: float) -> None:
    path = _cache_path()
    tmp = path.with_name('.tetradka_session.pending')
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            json.dump({'token': token, 'expires': expires, 'saved_at': time.time()}, file)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _phone_crm(raw: str) -> str:
    digits = re.sub(r'\D', '', str(raw))
    if len(digits) == 11 and digits[0] == '8':
        digits = '7' + digits[1:]
    if len(digits) == 10:
        digits = '7' + digits
    if len(digits) != 11 or not digits.startswith('7'):
        raise CRMAuthError('Номер CRM должен содержать 10 цифр.', code='phone_invalid')
    return f'+7 ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:11]}'


def _post_auth(opener, route: str, body: dict) -> dict:
    phase = 'login' if route == '/api/auth/login' else 'select_role'
    req = urllib.request.Request(CRM_ROOT + route,
        data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Accept': 'application/json'}, method='POST')
    try:
        with opener.open(req, timeout=16) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise CRMAuthError(f'Tetradka отклонила вход (HTTP {exc.code}).',
                           code=f'{phase}_http_{exc.code}') from None
    except (OSError, ValueError, TimeoutError) as exc:
        raise CRMAuthError('Не удалось связаться с авторизацией Tetradka.',
                           code=f'{phase}_network_{type(exc).__name__}') from None


def _ttl(token: str) -> float:
    fallback = time.time() + 8 * 3600
    try:
        middle = token.split('.')[1]
        middle += '=' * (-len(middle) % 4)
        exp = json.loads(base64.urlsafe_b64decode(middle)).get('exp')
        if isinstance(exp, (float, int)):
            return float(exp) - 180
    except (IndexError, ValueError, TypeError):
        pass
    return fallback


def note_verified_read(auth_header: str) -> None:
    global _TOKEN, _TOKEN_EXPIRES
    if os.environ.get('NEFOR_TETRADKA_TOKEN') or cfg('tetradka_token'):
        return
    if not isinstance(auth_header, str) or not auth_header.startswith('Bearer '):
        return
    token = auth_header[7:].strip()
    if not token or len(token) > 8192:
        return
    with _AUTH_LOCK:
        _TOKEN = token
        _TOKEN_EXPIRES = _ttl(token)
        _save_cached(token, _TOKEN_EXPIRES)


def bearer(*, force: bool = False) -> str:
    global _TOKEN
    external = cfg('tetradka_token') or os.environ.get('NEFOR_TETRADKA_TOKEN', '')
    if external:
        return external
    if not crm_configured():
        raise CRMAuthError('CRM Tetradka не подключена.', code='credentials_missing')
    with _AUTH_LOCK:
        if force:
            _TOKEN = ''
            _cache_path().unlink(missing_ok=True)
        if _TOKEN:
            return _TOKEN
        cached = _load_cached()
        if cached:
            _TOKEN = cached[0]
            return _TOKEN
        pwd = os.environ['NEFOR_TETRADKA_PASSWORD']
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        first = _post_auth(opener, '/api/auth/login', {
            'phone': _phone_crm(os.environ['NEFOR_TETRADKA_LOGIN']), 'password': pwd})
        roles = first.get('roles')
        if not isinstance(roles, list) or not roles:
            raise CRMAuthError('Tetradka не вернула ролей.', code='roles_missing')
        candidates = [r for r in roles if isinstance(r, dict) and r.get('type') == 'business']
        if len(candidates) != 1:
            raise CRMAuthError('Нужна единственная роль владельца.', code='business_role_count_invalid')
        uid = candidates[0].get('userID')
        if not uid:
            raise CRMAuthError('У роли владельца нет ID.', code='business_user_id_missing')
        second = _post_auth(opener, '/api/auth/v2/login-by-id', {'userID': uid, 'password': pwd})
        result = second.get('body', second.get('response', second))
        if not isinstance(result, dict):
            raise CRMAuthError('Некорректный ответ CRM.', code='select_role_invalid_response')
        if result.get('phoneActive') != 1:
            raise CRMAuthError('Tetradka требует подтверждения телефона.', code='phone_unconfirmed')
        if result.get('twoFactorActive') and not result.get('isTwoFactorPassed'):
            raise CRMAuthError('Tetradka требует 2FA.', code='two_factor_required')
        token = result.get('access_token')
        if not isinstance(token, str) or not token:
            raise CRMAuthError('Tetradka не выдала токен.', code='token_missing')
        _TOKEN = token
        _TOKEN_EXPIRES = _ttl(token)
        _save_cached(_TOKEN, _TOKEN_EXPIRES)
        return _TOKEN


def crm_headers(*, force: bool = False) -> dict:
    token = cfg('tetradka_token')
    if token:
        return {'Authorization': 'Bearer ' + token, 'Accept': '*/*', 'Connection': 'close'}
    return {'Authorization': 'Bearer ' + bearer(force=force), 'Accept': '*/*', 'Connection': 'close'}


def http_json(method: str, url: str, *, headers=None, params=None, payload=None, timeout=20):
    if params:
        url += ('&' if '?' in url else '?') + urllib.parse.urlencode(params)
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
    h = dict(headers or {})
    if data is not None:
        h['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        content = exc.read().decode('utf-8', 'replace')
        exc.close()
        try:
            return exc.code, json.loads(content)
        except ValueError:
            return exc.code, {'description': content[:400]}


# ─── Каталог ────────────────────────────────────────────────────────────────

_HIDDEN = re.compile(
    r'(?<![a-zа-яё0-9])(?:nef[eo]r[\s_-]*(?:salt|solt|sold)|'
    r'неф[ео]р[\s_-]*(?:салт|солт|солд|соль))(?![a-zа-яё0-9])', re.I)


def hidden_service(name) -> bool:
    return bool(_HIDDEN.search(str(name or '')))


def _booking_url(path: str) -> str:
    return f'{BASE_URL}/api/online/{ONLINE_ID}{path}'


def _parse_services(data) -> list:
    if not isinstance(data, dict) or not isinstance(data.get('categories'), list):
        raise BookingError('Не удалось получить услуги.', 502, 'upstream')
    found = {}
    for group in data['categories']:
        if not isinstance(group, dict):
            continue
        for s in group.get('services', []):
            if not isinstance(s, dict):
                continue
            if hidden_service(s.get('name')) or hidden_service(group.get('name')):
                continue
            try:
                sid = positive_id(s.get('serviceID'))
            except BookingError:
                continue
            p = s.get('price')
            found[sid] = {
                'id': sid, 'name': str(s.get('name') or '')[:160],
                'description': str(s.get('description') or '')[:250],
                'price': p if isinstance(p, (int, float)) and not isinstance(p, bool) else None,
                'minutes': s.get('time') if isinstance(s.get('time'), int) else None,
                'category': str(group.get('name') or '')[:80]}
    return list(found.values())


def _services_for(master_id: int) -> list:
    master_id = positive_id(master_id)
    q = urllib.parse.urlencode({'employeeID': master_id})
    code, data = _request_json('GET', _booking_url('/services') + '?' + q, None)
    if code != 200:
        raise BookingError('Услуги мастера недоступны.', 503, 'upstream')
    return _parse_services(data)


def catalog(*, cache: bool = True) -> dict:
    global _CATALOG_CACHE, _CATALOG_EXPIRES
    if cache and _CATALOG_CACHE is not None and time.monotonic() < _CATALOG_EXPIRES:
        return _CATALOG_CACHE
    with _CATALOG_LOCK:
        if cache and _CATALOG_CACHE is not None and time.monotonic() < _CATALOG_EXPIRES:
            return _CATALOG_CACHE
        code, raw = _request_json('GET', _booking_url('/employees-short'), None)
        if code != 200 or not isinstance(raw, list):
            raise BookingError('Не удалось получить мастеров.', 502, 'upstream')
        masters = []
        for m in raw:
            if not isinstance(m, dict):
                continue
            try:
                mid = positive_id(m.get('value'))
            except BookingError:
                continue
            masters.append({'id': mid, 'name': str(m.get('text') or '')[:150]})
        if not masters:
            raise BookingError('Список мастеров пуст.', 502, 'upstream')

        def safe(m):
            try:
                return _services_for(m['id'])
            except BookingError:
                return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(safe, masters))
        valid_masters = [m for m, items in zip(masters, results) if items]
        if not valid_masters:
            raise BookingError('Нет мастеров с услугами.', 503, 'upstream')
        merged = {}
        for m, items in zip(masters, results):
            if items is None:
                continue
            for s in items:
                service = merged.setdefault(s['id'], dict(s, master_ids=[], master_prices={}, master_minutes={}))
                service['master_ids'].append(m['id'])
                service['master_prices'][str(m['id'])] = s['price']
                service['master_minutes'][str(m['id'])] = s['minutes']
        result = {'masters': valid_masters,
                  'services': sorted(merged.values(), key=lambda x: (x['category'].lower(), x['name'].lower())),
                  'unbookable_masters': [m['name'] for m, items in zip(masters, results) if items == []]}
        if cache:
            _CATALOG_CACHE = result
            _CATALOG_EXPIRES = time.monotonic() + 600
        return result


def selection_check(master_id, service_id) -> str:
    mid = positive_id(master_id)
    sid = positive_id(service_id)
    items = _services_for(mid)
    if sid not in {s['id'] for s in items}:
        raise BookingError('Эта услуга недоступна у выбранного мастера.', 409, 'selection_unavailable')
    return f'{mid}:{sid}'


def available_dates(master_id, service_id) -> dict:
    pair = selection_check(master_id, service_id)
    today = datetime.now(MSK).date()
    q = {'dateFrom': today.isoformat(), 'dateTo': (today + timedelta(days=30)).isoformat(), 'pairs[]': [pair]}
    code, data = _request_json('GET',
        _booking_url('/available-dates') + '?' + urllib.parse.urlencode(q, doseq=True), None)
    if code != 200 or not isinstance(data, dict) or not isinstance(data.get('availableDates'), list):
        raise BookingError('Не удалось получить свободные даты.', 502, 'upstream')
    return {'dates': [x for x in data['availableDates'] if isinstance(x, str) and len(x) == 10]}


def available_slots(master_id, service_id, day: str) -> dict:
    check_date(day)
    pair = selection_check(master_id, service_id)
    q = {'date': day, 'pairs[]': [pair]}
    code, data = _request_json('GET',
        _booking_url('/available-time-slots') + '?' + urllib.parse.urlencode(q, doseq=True), None)
    if code != 200 or not isinstance(data, dict) or not isinstance(data.get('availableTimeSlots'), list):
        raise BookingError('Не удалось получить свободное время.', 502, 'upstream')
    return {'slots': [x for x in data['availableTimeSlots'] if isinstance(x, str) and len(x) == 5]}


# ─── Бронирование ───────────────────────────────────────────────────────────

def create_booking(payload: dict, *, verify_slot: bool = True) -> dict:
    validated = validate_booking(payload)
    pair = f"{validated['masterID']}:{validated['serviceID']}"
    if verify_slot:
        q = urllib.parse.urlencode({'date': validated['date'], 'pairs[]': pair})
        code, slot_data = _request_json('GET', _booking_url('/available-time-slots') + '?' + q, None)
        if code != 200 or not isinstance(slot_data, dict) or \
           not isinstance(slot_data.get('availableTimeSlots'), list):
            raise BookingError('Не удалось проверить свободное время.', 502, 'upstream')
        if validated['timeFrom'] not in slot_data['availableTimeSlots']:
            raise BookingError('Это время уже занято. Выберите другое.', 409, 'time_unavailable')
    body = {
        'nameClient': validated['nameClient'], 'phoneClient': validated['phoneClient'],
        'comment': validated['comment'],
        'visits': [{'date': validated['date'], 'timeFrom': validated['timeFrom'],
                    'bids': [{'masterID': validated['masterID'], 'serviceID': validated['serviceID']}]}],
        'pairs': [pair], 'date': validated['date'], 'timeFrom': validated['timeFrom'],
        'tetradkaPersonalDataProcessing': True}
    code, result = _request_json('POST', _booking_url('/store'), body)
    if code in (409, 422):
        raise BookingError('На это время не удалось записаться. Обновите расписание.', 409, 'time_unavailable')
    if code == 429:
        raise BookingError('Слишком много запросов. Попробуйте позднее.', 429, 'rate_limited')
    if code >= 500:
        raise BookingError('Ответ Tetradka неизвестен. Проверьте запись у администратора.', 503, 'uncertain')
    if code != 201:
        raise BookingError('Tetradka не подтвердила создание записи.', 502, 'upstream')
    if not isinstance(result, dict) or type(result.get('visitID')) is not int or result['visitID'] <= 0:
        raise BookingError('Ответ Tetradka неизвестен. Проверьте запись.', 503, 'uncertain')
    if type(result.get('addressID')) is not int:
        raise BookingError('Ответ Tetradka неизвестен. Проверьте запись.', 503, 'uncertain')
    return {'visitID': result['visitID'], 'addressID': result['addressID']}


# ─── Расписание ─────────────────────────────────────────────────────────────

def appointment_day(offset: int = 1) -> str:
    return (datetime.now(MSK) + timedelta(days=offset)).date().isoformat()


def extract_visits(reply) -> list:
    if isinstance(reply, dict):
        if reply.get('errors'):
            raise RuntimeError('Tetradka отклонила загрузку расписания')
        reply = reply.get('body', reply.get('response', reply))
    if isinstance(reply, list):
        return reply
    if not isinstance(reply, dict) or not any(k in reply for k in ('new', 'working', 'done')):
        raise RuntimeError('Tetradka: неожиданный формат расписания')
    out = []
    for section in ('new', 'working', 'done'):
        val = reply.get(section)
        if isinstance(val, list):
            out += [{**v, 'status': v.get('status') or section} for v in val if isinstance(v, dict)]
        elif isinstance(val, dict):
            for key in ('visits', 'items', 'data'):
                if isinstance(val.get(key), list):
                    out += [{**v, 'status': v.get('status') or section}
                            for v in val[key] if isinstance(v, dict)]
                    break
            else:
                raise RuntimeError('Tetradka: неполные данные расписания')
        elif section in reply:
            raise RuntimeError('Tetradka: неполные данные расписания')
    return out


def extract_item(v: dict) -> dict:
    client = v.get('client') or {}
    bids = v.get('bids') or []
    dt_str = str(v.get('dateTime') or v.get('date') or '')
    at = dt_str.split('T', 1)[1][:5] if 'T' in dt_str else '—'
    if at == '—' and bids:
        b = str(bids[0].get('timeFrom') or '')
        at = b.split('T', 1)[1][:5] if 'T' in b else b[:5] if ':' in b[:5] else '—'
    masters = list(dict.fromkeys(str(b['masterName']) for b in bids if b.get('masterName')))
    services = list(dict.fromkeys(str(b['serviceName']) for b in bids if b.get('serviceName')))
    return {'id': str(v.get('visitID') or ''), 'name': str(client.get('name') or 'Без имени'),
            'phone': norm_phone(client.get('phone')), 'time': at,
            'master': ', '.join(masters) or '—', 'service': ', '.join(services) or '—',
            'status': str(v.get('status') or ''),
            'notifications_disabled': bool(client.get('notificationsDisabled') or v.get('notificationsDisabled'))}


def schedule_failure_code(exc) -> str:
    if isinstance(exc, ScheduleFetchError):
        return exc.code
    if isinstance(exc, CRMAuthError):
        return 'crm_auth_' + re.sub(r'[^a-zA-Z0-9_]', '', exc.code)[:75]
    if isinstance(exc, TimeoutError):
        return 'network_timeout'
    if isinstance(exc, (OSError, ssl.SSLError)):
        return 'network_unavailable'
    return 'crm_unexpected_' + type(exc).__name__[:35]


def fetch_day(day: str, *, retries: int = 3) -> list:
    if not crm_configured():
        raise ScheduleFetchError('crm_credentials_missing')
    last = None
    for attempt in range(retries):
        try:
            hdr = crm_headers()
            code, data = http_json('GET', f'{CRM_ROOT}/api/v3/crm/visits',
                                   headers=hdr, params={'scheduled': 'all', 'date': day}, timeout=12)
            if code == 401 and not cfg('tetradka_token'):
                hdr = crm_headers(force=True)
                code, data = http_json('GET', f'{CRM_ROOT}/api/v3/crm/visits',
                                       headers=hdr, params={'scheduled': 'all', 'date': day}, timeout=12)
            if code != 200:
                raise ScheduleFetchError(f'crm_http_{code}')
            try:
                items = [extract_item(v) for v in extract_visits(data)
                         if str(v.get('status', '')).lower() not in ('cancel', 'cancelled', 'canceled')]
            except (TypeError, ValueError, KeyError, RuntimeError):
                raise ScheduleFetchError('crm_response_format') from None
            note_verified_read(hdr.get('Authorization', ''))
            return items
        except (OSError, TimeoutError, ssl.SSLError, CRMAuthError, ScheduleFetchError) as exc:
            last = exc
            cat = schedule_failure_code(exc)
            retriable = (cat.startswith('network_') or cat.startswith('crm_auth_login_http_422')
                         or cat in ('crm_http_429', 'crm_http_500', 'crm_http_502', 'crm_http_503', 'crm_http_504'))
            if not retriable or attempt == retries - 1:
                break
            time.sleep(.6 * (attempt + 1))
    raise last if isinstance(last, (ScheduleFetchError, CRMAuthError)) \
        else ScheduleFetchError(schedule_failure_code(last))


def fetch_clients(*, force: bool = False) -> list:
    if not crm_configured():
        raise RuntimeError('CRM Tetradka не подключена')
    h = crm_headers()
    clients = []
    total = None
    for _ in range(200):
        status, data = http_json('GET', f'{CRM_ROOT}/api/crm/v2/clients',
                                 headers=h, params={'skip': len(clients), 'limit': 100}, timeout=12)
        if status == 401 and not cfg('tetradka_token'):
            h = crm_headers(force=True)
            status, data = http_json('GET', f'{CRM_ROOT}/api/crm/v2/clients',
                                     headers=h, params={'skip': len(clients), 'limit': 100}, timeout=12)
        if status != 200 or (isinstance(data, dict) and data.get('errors')):
            raise RuntimeError(f'Tetradka clients HTTP {status}')
        if isinstance(data, dict):
            data = data.get('body', data.get('response', data))
        if isinstance(data, list):
            batch = data
        elif isinstance(data, dict) and any(isinstance(data.get(k), list) for k in ('clients', 'items', 'data')):
            batch = next(data[k] for k in ('clients', 'items', 'data') if isinstance(data.get(k), list))
            if type(data.get('count')) is int:
                total = data['count']
        else:
            raise RuntimeError('Tetradka: неожиданный формат базы клиентов')
        if any(not isinstance(c, dict) for c in batch):
            raise RuntimeError('Tetradka: неполные данные клиентов')
        clients.extend(batch)
        if total is not None and len(clients) >= total:
            break
        if not batch:
            if total is not None and len(clients) < total:
                raise RuntimeError('Tetradka: база клиентов загружена частично')
            break
        if total is None and len(batch) < 100:
            break
    else:
        raise RuntimeError('Tetradka: база клиентов загружена частично')
    return clients