"""Клиентские операции с визитами: owner, revision, cancel, reschedule."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json, re, time

import crm
import storage as store
from crm import BookingError, CRM_ROOT

MSK = timezone(timedelta(hours=3))


def _request(method: str, route: str, body=None):
    try:
        code, data = crm.http_json(method, f'{CRM_ROOT}/api/{route}',
                                   headers=crm.crm_headers(), payload=body, timeout=12)
        if method == 'GET' and code == 401:
            code, data = crm.http_json(method, f'{CRM_ROOT}/api/{route}',
                                       headers=crm.crm_headers(force=True), payload=None, timeout=12)
    except Exception:
        raise BookingError('Tetradka не ответила. Результат нужно проверить.', 503, 'upstream') from None
    return code, data


def _unwrap(data):
    if isinstance(data, dict):
        if data.get('errors'):
            raise BookingError('Tetradka отклонила запрос.', 502, 'upstream')
        return data.get('body', data.get('response', data))
    return data


def _read_visit(vid: int) -> dict:
    code, data = _request('GET', f'v3/crm/visits/{int(vid)}')
    if code != 200:
        raise BookingError('Не удалось проверить визит в Tetradka.', 503, 'upstream')
    v = _unwrap(data)
    if not isinstance(v, dict) or str(v.get('visitID')) != str(vid) or not isinstance(v.get('bids'), list):
        raise BookingError('Tetradka вернула неполные данные визита.', 502, 'upstream')
    return v


def _owner(uid: int, v: dict) -> bool:
    phone = store.norm_phone((v.get('client') or {}).get('phone'))
    if store.ledger_owns(uid, v['visitID'], phone):
        return True
    p = store.profile(uid) or {}
    return bool(phone and p.get('phone_verified') and store.norm_phone(p.get('phone')) == phone)


def _revision(v: dict) -> str:
    data = {k: v.get(k) for k in ('visitID', 'clientID', 'date', 'dateTime',
                                   'status', 'bids', 'repeat', 'sale', 'comment')}
    data['phone'] = store.norm_phone((v.get('client') or {}).get('phone'))
    return sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def _when(v: dict):
    day = str(v.get('date') or v.get('dateTime') or '')[:10]
    bids = v.get('bids') or []
    times = [str(b.get('timeFrom', '')).split('T')[-1][:5] for b in bids]
    at = min(times) if times else ''
    try:
        datetime.fromisoformat(day + 'T' + at)
    except ValueError:
        raise BookingError('Не удалось определить время визита.', 502, 'upstream') from None
    return day, at


def _capabilities(v: dict):
    day, at = _when(v)
    future = datetime.fromisoformat(day + 'T' + at).replace(tzinfo=MSK) > datetime.now(MSK)
    cancel = future and v.get('status') == 'new' and not v.get('sale')
    move = cancel and len(v['bids']) == 1 and not v.get('repeat')
    reason = '' if move else (
        'Перенос составных, повторяющихся или оплаченных визитов согласуй с администратором.'
        if cancel else 'Изменение этого визита согласуй с администратором.')
    return cancel, move, reason


def _public(uid: int, v: dict) -> dict:
    if not _owner(uid, v):
        raise BookingError('Запись не найдена.', 404, 'ownership')
    day, at = _when(v)
    cancel, move, reason = _capabilities(v)
    ref = store.ref_create(uid, int(v['visitID']), _revision(v))
    return {'ref': ref, 'visitID': v['visitID'], 'date': day, 'time': at,
            'status': v.get('status'),
            'master': ', '.join(dict.fromkeys(str(b.get('masterName') or '') for b in v['bids'])),
            'service': ', '.join(dict.fromkeys(str(b.get('serviceName') or '') for b in v['bids'])),
            'fresh': True, 'source': 'crm',
            'can_cancel': cancel, 'can_reschedule': move, 'reason': reason}


def _resolve(uid: int, ref: str, check_revision: bool = True) -> dict:
    row = store.ref_get(uid, ref)
    if not row:
        raise BookingError('Обнови список записей.', 404, 'reference')
    v = _read_visit(row['visit_id'])
    if not _owner(uid, v):
        raise BookingError('Запись не найдена.', 404, 'ownership')
    if check_revision and _revision(v) != row['revision']:
        raise BookingError('Запись изменилась в CRM. Обнови список.', 409, 'changed')
    return v


def my_visits(uid: int) -> dict:
    local = store.ledger_recent(uid)
    p = store.profile(uid) or {}
    verified = bool(p.get('phone_verified') and p.get('phone'))
    raw = {}
    failed = False
    try:
        crm.crm_headers()
        if verified:
            phone = store.norm_phone(p['phone'])
            days = [(datetime.now(MSK).date() + timedelta(days=i)).isoformat() for i in range(30)]
            from concurrent.futures import ThreadPoolExecutor

            def read_day(day):
                code, data = _request('GET', 'v3/crm/visits?scheduled=all&date=' + day)
                if code != 200:
                    raise BookingError('Расписание не загружено.', 503, 'upstream')
                return crm.extract_visits(_unwrap(data))

            with ThreadPoolExecutor(max_workers=6) as pool:
                jobs = [pool.submit(read_day, d) for d in days]
                for job in jobs:
                    try:
                        for v in job.result():
                            if store.norm_phone((v.get('client') or {}).get('phone')) == phone:
                                raw[int(v['visitID'])] = v
                    except Exception:
                        failed = True
        for item in local:
            try:
                v = _read_visit(item['visitID'])
                if _owner(uid, v):
                    raw[int(v['visitID'])] = v
            except Exception:
                failed = True
    except Exception:
        failed = True
    visits = []
    for v in raw.values():
        try:
            visits.append(_public(uid, _read_visit(v['visitID'])))
        except BookingError:
            failed = True
    found = {str(v['visitID']) for v in visits}
    if failed:
        visits.extend({**v, 'fresh': False, 'status': 'not_checked',
                       'can_cancel': False, 'can_reschedule': False}
                      for v in local if str(v['visitID']) not in found)
    pending = [_result(r) for r in store.action_pending_for(uid)]
    return {'visits': sorted(visits, key=lambda v: (v['date'], v['time'])),
            'pending_actions': pending,
            'requires_phone': not verified,
            'fresh': not failed,
            'complete': verified and not failed,
            'source': 'crm' if not failed else 'partial',
            'message': 'Расписание не загружено полностью. Список может быть неполным.' if failed else '',
            'fetched_at': datetime.now(MSK).isoformat(timespec='seconds')}


def _selection(v: dict):
    _, move, _ = _capabilities(v)
    if not move:
        raise BookingError('Для переноса этого визита напиши администратору.', 409, 'unsupported')
    b = v['bids'][0]
    return int(b['masterID']), int(b['serviceID'])


def calendar(uid: int, ref: str, day: str = '') -> dict:
    v = _resolve(uid, ref)
    mid, sid = _selection(v)
    return crm.available_slots(mid, sid, day) if day else crm.available_dates(mid, sid)


def _result(row: dict) -> dict:
    state = row['state']
    if state == 'done':
        msg = 'Отмена подтверждена Tetradka.' if row['action'] == 'cancel' else 'Перенос подтверждён Tetradka.'
    elif state == 'rejected':
        msg = 'Tetradka отклонила изменение. Обнови записи.'
    else:
        msg = 'Результат пока не подтверждён CRM. Не повторяй действие; нажми «Проверить результат».'
    return {'ok': state == 'done', 'confirmed': state == 'done', 'state': state,
            'requestKey': row['request_key'], 'action': row['action'], 'message': msg}


def _matches(row: dict, v: dict) -> bool:
    if row['action'] == 'cancel':
        return v.get('status') == 'cancel'
    if v.get('status') != 'new' or _when(v) != (row['target_date'], row['target_time']):
        return False
    expected = json.loads(row['result']) if row['result'] else None
    if not expected:
        return False
    if str(v.get('clientID')) != str(expected['clientID']) or (v.get('comment') or '') != expected['comment']:
        return False
    if len(v['bids']) != len(expected['bids']):
        return False
    for actual, want in zip(v['bids'], expected['bids']):
        if any(str(actual.get(k)) != str(want[k]) for k in ('masterID', 'serviceID', 'timeDuration')):
            return False
        if float(actual.get('price', -1)) != float(want['price']):
            return False
        if (actual.get('priceTypeAlias') or 'fixed') != want['priceType']:
            return False
    return True


def _reconcile(uid: int, row: dict) -> dict:
    if row['state'] not in ('pending', 'uncertain'):
        return _result(row)
    try:
        v = _read_visit(row['visit_id'])
        if not _owner(uid, v):
            return _result(row)
        if _matches(row, v):
            with store.db(store.BOOKING_DB) as con:
                con.execute("UPDATE visit_actions SET state='done' WHERE telegram_id=? AND request_key=?",
                            (uid, row['request_key']))
            if row['action'] == 'reschedule':
                store.ledger_sync_visit(v['visitID'], *_when(v))
            if row['action'] == 'cancel':
                # Снимаем блокировку ledger: клиент может записаться заново
                # на то же время, ту же услугу и того же мастера.
                store.ledger_cancel(v['visitID'])
            row = {**dict(row), 'state': 'done'}
    except Exception:
        pass
    return _result(row)


def action_status(uid: int, key: str) -> dict:
    row = store.action_get(uid, key)
    if not row:
        raise BookingError('Действие не найдено.', 404, 'action')
    return _reconcile(uid, row)


def change(uid: int, data: dict) -> dict:
    if data.get('confirmed') is not True:
        raise BookingError('Подтверди действие.', 400, 'confirmation')
    key = data.get('requestKey')
    kind = data.get('action')
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,100}', key) \
            or kind not in ('cancel', 'reschedule'):
        raise BookingError('Неверное действие.', 400, 'action')
    day = str(data.get('date') or '') if kind == 'reschedule' else ''
    at = str(data.get('time') or '') if kind == 'reschedule' else ''

    previous = store.action_get(uid, key)
    if previous:
        v = _resolve(uid, data.get('ref'), check_revision=False)
        if (previous['visit_id'], previous['action'], previous['target_date'], previous['target_time']) != \
           (int(v['visitID']), kind, day, at):
            raise BookingError('Ключ действия уже использован.', 409, 'request_key')
        return _reconcile(uid, previous)

    v = _resolve(uid, data.get('ref'))
    cancel, move, _ = _capabilities(v)
    if not cancel or (kind == 'reschedule' and not move):
        raise BookingError('Изменение этого визита согласуй с администратором.', 409, 'unsupported')

    if kind == 'cancel':
        route = f"crm/v2/visits/{v['visitID']}/status"
        body = {'_method': 'put', 'status': 'cancel'}
    else:
        mid, sid = _selection(v)
        crm.check_date(day)
        if not re.fullmatch(r'\d{2}:\d{2}', at):
            raise BookingError('Выбери время.', 400, 'time')
        if datetime.fromisoformat(day + 'T' + at).replace(tzinfo=MSK) <= datetime.now(MSK):
            raise BookingError('Это время уже прошло.', 409, 'time')
        if _when(v) == (day, at):
            raise BookingError('Выбери новое время.', 409, 'time')
        if at not in crm.available_slots(mid, sid, day)['slots']:
            raise BookingError('Время уже занято. Выбери другое.', 409, 'time_unavailable')
        b = v['bids'][0]
        from urllib.parse import urlencode
        for warning in ('service-resources', 'service-time'):
            q = {'date': day, 'time': at, 'timeDuration': b['timeDuration'],
                 'serviceID': sid, 'visitID': v['visitID']}
            if warning == 'service-time':
                q['masterID'] = mid
            code, available = _request('GET', 'crm/v2/bids/' + warning + '?' + urlencode(q))
            available = _unwrap(available)
            if code != 200 or not isinstance(available, dict) or available.get('isFree') is not True:
                raise BookingError('Не удалось подтвердить доступность времени.', 409, 'time_unavailable')
        route = f"v3/crm/visits/{v['visitID']}"
        body = {'date': day, 'comment': v.get('comment') or '', 'clientID': v['clientID'], 'repeat': None,
                'bids': [{'masterID': mid, 'serviceID': sid, 'timeFrom': at,
                          'timeDuration': b['timeDuration'], 'price': int(b['price']),
                          'priceType': b.get('priceTypeAlias') or 'fixed'}]}

    if _revision(_read_visit(v['visitID'])) != _revision(v):
        raise BookingError('Запись изменилась. Обнови список.', 409, 'changed')

    active = store.action_active_for_visit(int(v['visitID']))
    if active:
        if active['telegram_id'] == uid:
            return _result(active)
        raise BookingError('Предыдущее действие ещё проверяется.', 409, 'uncertain')

    store.action_insert(uid, key, int(v['visitID']), kind, day, at,
                        json.dumps(body, ensure_ascii=False))

    state = 'uncertain'
    try:
        code, reply = _request('PUT', route, body)
        if 400 <= code < 500 or (isinstance(reply, dict) and reply.get('errors')):
            state = 'rejected'
    except Exception:
        pass
    store.action_update_state(uid, key, state)
    row = store.action_get(uid, key)
    if state == 'rejected':
        raise BookingError('Tetradka отклонила изменение. Обнови записи.', 409, 'rejected')
    return _reconcile(uid, row)
