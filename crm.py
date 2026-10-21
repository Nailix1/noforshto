"""Интеграция с собственной CRM (app.py)."""
from __future__ import annotations
import json, time, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone

from config import cfg
from storage import norm_phone

MSK = timezone(timedelta(hours=3))

class BookingError(Exception):
    def __init__(self, message: str, status_code: int = 400, code: str = 'invalid'):
        super().__init__(message)
        self.status_code = status_code
        self.code = code

# Кеш состояния CRM, чтобы не спамить запросами
_CACHE = {'at': 0, 'data': None}
_CACHE_TTL = 5  # секунд


def _crm_url() -> str:
    url = str(cfg('crm_url', 'http://127.0.0.1:4011')).strip().rstrip('/')
    return url


def _req(path: str, method: str = 'GET', body: dict = None) -> dict:
    url = _crm_url() + path
    data = None if body is None else json.dumps(body).encode('utf-8')
    headers = {'Content-Type': 'application/json'} if data else {}
    
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            err_data = json.loads(e.read().decode('utf-8'))
            err_msg = err_data.get('error', 'Ошибка CRM')
        except Exception:
            err_msg = f'HTTP {e.code}'
        raise BookingError(err_msg, e.code, err_msg)
    except Exception as e:
        print(f'[CRM_NETWORK_ERROR] {type(e).__name__}', flush=True)
        raise BookingError('Сервер CRM временно недоступен', 503, 'network')


def get_state(force: bool = False) -> dict:
    now = time.time()
    if not force and _CACHE['data'] and now - _CACHE['at'] < _CACHE_TTL:
        return _CACHE['data']
    try:
        data = _req('/api/state')
        _CACHE['data'] = data.get('data', {})
        _CACHE['at'] = now
        return _CACHE['data']
    except Exception:
        if _CACHE['data']:
            return _CACHE['data']
        raise


def crm_configured() -> bool:
    return True


# ─── Утилиты для времени ───────────────────────────────────────────────────

def to_min(t: str) -> int:
    try:
        h, m = t.split(':')[:2]
        return int(h) * 60 + int(m)
    except Exception:
        return 0


def from_min(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def _hours_for(state: dict, master: dict, date_str: str):
    if date_str in (state.get('settings', {}).get('closed') or []):
        return None
    ex = (master.get('exceptions') or {}).get(date_str)
    if ex:
        if ex.get('off'):
            return None
        return {'from': ex.get('from'), 'to': ex.get('to'), 'bf': ex.get('bf', ''), 'bt': ex.get('bt', '')}
    try:
        dt = datetime.strptime(date_str, '%Y-%m-%d')
    except Exception:
        return None
    js_dow = (dt.weekday() + 1) % 7
    w = (master.get('week') or {}).get(str(js_dow))
    if not w:
        return None
    return w if w.get('on') else None


def _overlaps(state: dict, mid: str, date_str: str, start: int, dur: int, ignore_id: str = None) -> bool:
    for a in state.get('appts', []):
        if a.get('id') == ignore_id or a.get('masterId') != mid or a.get('date') != date_str:
            continue
        if a.get('status') == 'cancelled':
            continue
        a_start = to_min(a.get('time'))
        a_end = a_start + int(a.get('dur') or 0)
        if start < a_end and start + dur > a_start:
            return True
    return False


def _free_slots(state: dict, master: dict, date_str: str, dur: int) -> list:
    h = _hours_for(state, master, date_str)
    today_str = datetime.now(MSK).date().isoformat()
    
    if not h or date_str < today_str:
        return []
    
    settings = state.get('settings', {})
    step = int(settings.get('step', 30))
    f = to_min(h.get('from') or '')
    t = to_min(h.get('to') or '')
    bf = to_min(h.get('bf') or '')
    bt = to_min(h.get('bt') or '')
    
    out = []
    n = datetime.now(MSK)
    lim = n.hour * 60 + n.minute + int(settings.get('minAhead', 0)) if date_str == today_str else -1
    
    for x in range(f, t - dur + 1, step):
        if bf >= 0 and bt > bf and x < bt and x + dur > bf:
            continue
        if x < lim or _overlaps(state, master.get('id'), date_str, x, dur):
            continue
        out.append(from_min(x))
    return out


# ─── Каталог (Мастера и Услуги) ────────────────────────────────────────────

def catalog(*, cache: bool = True) -> dict:
    state = get_state(force=not cache)
    raw_masters = state.get('masters', [])
    raw_services = state.get('services', [])

    masters = []
    for m in raw_masters:
        if m.get('online', True):
            masters.append({'id': str(m['id']), 'name': str(m.get('name', ''))})

    services = []
    for s in raw_services:
        if not s.get('online', True):
            continue
        # Находим мастеров, которые умеют делать эту услугу
        master_ids = [str(m['id']) for m in raw_masters if s['id'] in m.get('services', [])]
        
        services.append({
            'id': str(s['id']),
            'name': str(s.get('name', '')),
            'category': str(s.get('cat', 'Услуги')),
            'price': float(s.get('price', 0)),
            'minutes': int(s.get('dur', 60)),
            'master_ids': master_ids
        })

    return {'masters': masters, 'services': services}


# ─── Даты и Слоты ──────────────────────────────────────────────────────────

def available_dates(master_id: str, service_id: str) -> dict:
    state = get_state()
    s = next((x for x in state.get('services', []) if str(x['id']) == str(service_id)), None)
    if not s:
        raise BookingError('Услуга не найдена', 404)
    
    dur = int(s.get('dur', 60))
    today = datetime.now(MSK).date()
    horizon = int(state.get('settings', {}).get('horizon', 30))
    dates = []
    
    for i in range(horizon + 1):
        d_str = (today + timedelta(days=i)).isoformat()
        if master_id == 'any':
            # Проверяем, есть ли слоты хотя бы у одного мастера
            for m in state.get('masters', []):
                if str(m['id']) in [str(mid) for mid in s.get('master_ids', [])]:
                    if _free_slots(state, m, d_str, dur):
                        dates.append(d_str)
                        break
        else:
            m = next((x for x in state.get('masters', []) if str(x['id']) == str(master_id)), None)
            if m and _free_slots(state, m, d_str, dur):
                dates.append(d_str)
                
    return {'dates': dates}


def available_slots(master_id: str, service_id: str, day: str) -> dict:
    state = get_state()
    s = next((x for x in state.get('services', []) if str(x['id']) == str(service_id)), None)
    if not s:
        raise BookingError('Услуга не найдена', 404)
    dur = int(s.get('dur', 60))
    
    slots = set()
    if master_id == 'any':
        for m in state.get('masters', []):
            if str(m['id']) in [str(mid) for mid in s.get('master_ids', [])]:
                slots.update(_free_slots(state, m, day, dur))
    else:
        m = next((x for x in state.get('masters', []) if str(x['id']) == str(master_id)), None)
        if m:
            slots.update(_free_slots(state, m, day, dur))
            
    return {'slots': sorted(list(slots))}


# ─── Создание записи ───────────────────────────────────────────────────────

def create_booking(payload: dict) -> dict:
    # payload приходит из server.py с фронтенда
    body = {
        'serviceId': payload['serviceID'],
        'masterId': payload['masterID'],
        'date': payload['date'],
        'time': payload['timeFrom'],
        'name': payload['nameClient'],
        'phone': payload['phoneClient'],
        'comment': payload.get('comment', '')
    }
    
    try:
        res = _req('/api/book', method='POST', body=body)
        _CACHE['at'] = 0 # Сбрасываем кеш, чтобы сразу увидеть новую запись
        return {'visitID': res['appt']['id'], 'addressID': 1}
    except BookingError as e:
        if e.status_code == 409:
            raise BookingError('Это время уже занято', 409, 'time_unavailable')
        raise e


# ─── Расписание для админки ────────────────────────────────────────────────

def fetch_day(day: str) -> list:
    state = get_state(force=True)
    masters_map = {str(m['id']): m['name'] for m in state.get('masters', [])}
    services_map = {str(s['id']): s['name'] for s in state.get('services', [])}
    
    res = []
    for a in state.get('appts', []):
        if a.get('date') != day or a.get('block'):
            continue
        if a.get('status') == 'cancelled':
            continue
            
        res.append({
            'id': str(a['id']),
            'name': str(a.get('client', 'Без имени')),
            'phone': norm_phone(a.get('phone', '')),
            'time': a.get('time'),
            'master': masters_map.get(str(a.get('masterId')), '—'),
            'service': services_map.get(str(a.get('serviceId')), '—'),
            'status': a.get('status'),
            'notifications_disabled': False
        })
    return res
