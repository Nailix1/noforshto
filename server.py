"""NEFOR SPOT Mini App HTTP + API."""
from __future__ import annotations
import json, mimetypes, os, re, threading, time
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, parse_qsl, urlsplit

import crm, storage as store, visits, chatpush, notify, telegram
from config import WEB_DIR, cfg

MSK = timezone(timedelta(hours=3))
MAX_BODY = 16_384
SERVER_VERSION = 'NEFORSPOT-MiniApp/9.4'

_IOS_PHASES = frozenset(('inline', 'dom', 'sdk-ready', 'sdk-missing', 'signed', 'unsigned',
                          'app-ready', 'app-failed', 'js-error', 'touch-main', 'touch-nav',
                          'click-main', 'click-nav', 'handler-main', 'handler-nav',
                          'catalog-received', 'catalog-rendered', 'catalog-render-failed',
                          'action-failed', 'nav-render-failed'))
_IOS_LOCK = threading.Lock()
_IOS_WINDOW = 0.0
_IOS_COUNT = 0


class HTTPFailure(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def verify_init_data(init_data: str, bot_token: str, *, now=None, ttl: int = 86400) -> dict:
    if not bot_token:
        raise HTTPFailure(401, 'Авторизация Telegram не настроена.')
    if not isinstance(init_data, str) or not init_data or len(init_data) > 8192:
        raise HTTPFailure(401, 'Открой приложение из Telegram.')
    try:
        raw = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise HTTPFailure(401, 'Некорректные данные Telegram.') from exc
    pairs = {}
    for k, v in raw:
        if k in pairs:
            raise HTTPFailure(401, 'Некорректная подпись.')
        pairs[k] = v
    signature = pairs.pop('hash', None)
    if not signature or not re.fullmatch('[0-9a-fA-F]{64}', signature):
        raise HTTPFailure(401, 'Нет подписи Telegram.')
    check = '\n'.join(f'{k}={pairs[k]}' for k in sorted(pairs))
    secret = hmac.new(b'WebAppData', bot_token.encode('utf-8'), sha256).digest()
    actual = hmac.new(secret, check.encode('utf-8'), sha256).hexdigest()
    if not hmac.compare_digest(signature.lower(), actual):
        raise HTTPFailure(401, 'Неверная подпись Telegram.')
    try:
        issued = int(pairs['auth_date'])
        user = json.loads(pairs['user'])
        uid = int(user['id'])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise HTTPFailure(401, 'Не хватает данных авторизации.')
    at = int(time.time()) if now is None else now
    if issued > at + 120 or issued < at - ttl:
        raise HTTPFailure(401, 'Авторизация устарела. Переоткрой приложение.')
    if uid <= 0:
        raise HTTPFailure(401, 'Неверный пользователь.')
    return user


def user_from_headers(headers, mode: str) -> dict:
    if mode not in ('client', 'admin'):
        raise HTTPFailure(400, 'Неверный режим.')
    token = cfg('bot_token' if mode == 'admin' else 'client_bot_token')
    user = verify_init_data(headers.get('X-Telegram-Init-Data', ''), token)
    if mode == 'admin' and int(user['id']) != int(cfg('admin_id') or 0):
        raise HTTPFailure(403, f'Этот аккаунт (ID {user["id"]}) не в списке администраторов.')
    return user


def boot_public(mode: str, user: dict) -> dict:
    return {'mode': mode,
            'user': {'id': user['id'], 'first_name': user.get('first_name', ''),
                     'username': user.get('username', '')},
            'brand': 'NEFOR SPOT',
            'address': cfg('salon_address', 'Разъезжая 42/34'),
            'booking_url': cfg('booking_url', ''),
            'hours': 'Ежедневно, 12:00–22:00', 'bot': 'NEFORSPOTBOT',
            'notice': 'Выбери услугу, мастера, дату и время. Визит подтверждает Tetradka.',
            'version': '9.4',
            'booking_enabled': cfg('booking_enabled', True)}


def booking_comment_with_telegram(comment: str, user: dict) -> str:
    username = str(user.get('username') or '')
    username = username if re.fullmatch(r'[A-Za-z0-9_]{5,32}', username) else ''
    uid = int(user['id'])
    lines = [('Telegram: @' + username) if username else 'Telegram: без @username']
    if username:
        lines.append('Профиль: https://t.me/' + username)
    lines.append('Telegram ID: ' + str(uid))
    if comment.strip():
        lines.append('Пожелания: ' + comment.strip())
    result = '\n'.join(lines)
    if len(result) > 500:
        raise crm.BookingError('Слишком длинный комментарий.')
    return result


def tomorrow_payload() -> dict:
    day = crm.appointment_day()
    try:
        rows = crm.fetch_day(day)
        store.save_visits(day, rows)
        fresh = True
        diagnostic = 'ok'
    except Exception as exc:
        rows = store.rows_for_date(day)
        fresh = False
        diagnostic = crm.schedule_failure_code(exc)
    return {'day': day, 'fresh': fresh, 'source': 'crm' if fresh else 'saved',
            'diagnostic_code': diagnostic,
            'chatpush_connected': chatpush.configured(),
            'chatpush_history': chatpush.history_connection_status(),
            'visits': rows}


class Server(BaseHTTPRequestHandler):
    server_version = SERVER_VERSION

    def log_message(self, fmt, *args):
        print('[MiniApp]', self.command, urlsplit(self.path).path, flush=True)

    def send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_file(self):
        raw = urlsplit(self.path).path
        rel = raw.lstrip('/') or 'index.html'
        target = (WEB_DIR / rel).resolve()
        if not str(target).startswith(str(WEB_DIR.resolve())) or not target.is_file():
            raise HTTPFailure(404, 'Файл не найден.')
        data = target.read_bytes()
        kind = mimetypes.guess_type(str(target))[0] or 'application/octet-stream'
        ctype = kind + ('; charset=utf-8' if kind.startswith('text/')
                        or kind == 'application/javascript' else '')
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy',
                         "frame-ancestors 'self' https://web.telegram.org "
                         "https://*.telegram.org https://t.me https://*.t.me")
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def user(self, mode): return user_from_headers(self.headers, mode)

    def payload(self):
        try:
            n = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            raise HTTPFailure(400, 'Ошибка размера.')
        if n < 0 or n > MAX_BODY:
            raise HTTPFailure(413, 'Слишком большой запрос.')
        try:
            return json.loads(self.rfile.read(n).decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            raise HTTPFailure(400, 'Некорректный JSON.')

    def fail(self, error):
        self.send_json(error.status if isinstance(error, HTTPFailure) else 500,
                       {'error': str(error) if isinstance(error, HTTPFailure)
                        else 'Временная ошибка сервера.'})

    def do_GET(self):
        try:
            path = urlsplit(self.path).path
            if path.startswith('/api/ios-probe/'):
                return self._ios_probe(path)
            if not path.startswith('/api/'):
                return self.serve_file()
            if path == '/api/health':
                return self.send_json(200, {'ok': True, 'version': '9.4',
                                            'time': datetime.now(MSK).isoformat(),
                                            'booking_mode': 'direct'})
            mode = self.headers.get('X-App-Mode', 'client')
            user = self.user(mode)
            uid = int(user['id'])

            if path == '/api/bootstrap':
                info = boot_public(mode, user)
                if mode == 'client':
                    store.upsert_profile(uid, user.get('username', ''),
                        (user.get('first_name', '') + ' ' + user.get('last_name', '')).strip())
                    info['profile'] = store.public_profile(uid)
                return self.send_json(200, info)

            if path == '/api/client/profile' and mode == 'client':
                return self.send_json(200, store.public_profile(uid))

            if path in ('/api/client/visit-calendar', '/api/client/visit-action-status') and mode == 'client':
                q = parse_qs(urlsplit(self.path).query)
                try:
                    if path.endswith('visit-calendar'):
                        info = visits.calendar(uid, q.get('ref', [''])[0], q.get('date', [''])[0])
                    else:
                        info = visits.action_status(uid, q.get('requestKey', [''])[0])
                except crm.BookingError as exc:
                    raise HTTPFailure(exc.status_code, str(exc)) from None
                return self.send_json(200, info)

            if path.startswith('/api/booking/') and mode == 'client':
                if not cfg('booking_enabled', True):
                    raise HTTPFailure(503, 'Онлайн-запись временно отключена.')
                q = parse_qs(urlsplit(self.path).query)
                def q_(n): return q.get(n, [''])[0]
                try:
                    if path == '/api/booking/catalog':
                        return self.send_json(200, crm.catalog())
                    if path == '/api/booking/dates':
                        return self.send_json(200, crm.available_dates(q_('master_id'), q_('service_id')))
                    if path == '/api/booking/slots':
                        return self.send_json(200, crm.available_slots(q_('master_id'), q_('service_id'), q_('date')))
                    if path == '/api/booking/recent':
                        return self.send_json(200, {'visits': store.ledger_recent(uid)})
                except crm.BookingError as exc:
                    raise HTTPFailure(exc.status_code, str(exc))
                raise HTTPFailure(404, 'Неизвестный маршрут.')

            if path == '/api/client/tickets' and mode == 'client':
                return self.send_json(200, {'tickets': store.own_tickets(uid)})

            if path == '/api/client/ticket-messages' and mode == 'client':
                try:
                    tid = int(parse_qs(urlsplit(self.path).query).get('ticket_id', [''])[0])
                except (ValueError, TypeError):
                    raise HTTPFailure(400, 'Неверный номер.')
                t = store.ticket(tid)
                if not t or int(t['telegram_id']) != uid:
                    raise HTTPFailure(404, 'Не найдено.')
                return self.send_json(200, {'ticket_id': tid, 'status': t['status'],
                                            'messages': store.conversation(tid)})

            if path == '/api/client/my-visits' and mode == 'client':
                return self.send_json(200, visits.my_visits(uid))

            if path == '/api/admin/crm-status' and mode == 'admin':
                if not crm.crm_configured():
                    return self.send_json(200, {'configured': False, 'authenticated': False,
                                                'message': 'Tetradka не настроена.'})
                try:
                    crm.fetch_day(crm.appointment_day())
                    return self.send_json(200, {'configured': True, 'authenticated': True,
                                                'message': 'Связь проверена.'})
                except Exception as exc:
                    code = exc.code if isinstance(exc, crm.CRMAuthError) else crm.schedule_failure_code(exc)
                    return self.send_json(200, {'configured': True, 'authenticated': False,
                                                'message': 'CRM недоступна.',
                                                'diagnostic_code': code})

            if path == '/api/admin/tomorrow' and mode == 'admin':
                return self.send_json(200, tomorrow_payload())

            if path == '/api/admin/chatpush/status' and mode == 'admin':
                info = chatpush.auth_status()
                info['auto_enabled'] = False
                info['external_history'] = chatpush.history_connection_status()
                return self.send_json(200, info)

            if path == '/api/admin/notifications' and mode == 'admin':
                return self.send_json(200, {'messages': notify.entries(80),
                                            'manual_count': notify.manual_count()})

            if path == '/api/admin/bookings' and mode == 'admin':
                return self.send_json(200, {'bookings': store.ledger_admin_recent()})

            if path == '/api/admin/tickets' and mode == 'admin':
                return self.send_json(200, {'tickets': store.tickets('new', 30)})

            if path == '/api/admin/ticket-messages' and mode == 'admin':
                try:
                    tid = int(parse_qs(urlsplit(self.path).query).get('ticket_id', [''])[0])
                except (ValueError, TypeError):
                    raise HTTPFailure(400, 'Неверный номер.')
                if not store.ticket(tid):
                    raise HTTPFailure(404, 'Не найдено.')
                return self.send_json(200, {'ticket_id': tid, 'messages': store.conversation(tid)})

            if path == '/api/admin/clients' and mode == 'admin':
                if not crm.crm_configured():
                    raise HTTPFailure(503, 'CRM недоступна.')
                try:
                    raw = crm.fetch_clients()
                except Exception:
                    raise HTTPFailure(503, 'База клиентов не загружена.')
                safe = [{'id': str(c.get('clientID') or ''), 'name': str(c.get('name') or ''),
                         'phone': str(c.get('phone') or ''),
                         'last_visit': str(c.get('lastVisit') or '')[:10]} for c in raw]
                return self.send_json(200, {'clients': safe, 'fresh': True, 'source': 'crm'})

            raise HTTPFailure(404, 'Раздел не найден.')
        except HTTPFailure as ex:
            self.fail(ex)
        except Exception as ex:
            print('[MiniApp] GET error:', type(ex).__name__, flush=True)
            self.fail(ex)

    def _ios_probe(self, path):
        global _IOS_WINDOW, _IOS_COUNT
        stage = path[len('/api/ios-probe/'):]
        category = re.fullmatch(
            r'error-(app|sdk|other)-(type|reference|syntax|range|other)-l(?:0|[1-9][0-9]{0,3})', stage)
        rejected = re.fullmatch(r'rejection-(type|reference|syntax|range|other)', stage)
        if stage not in _IOS_PHASES and category is None and rejected is None:
            raise HTTPFailure(404, 'Неизвестный этап.')
        with _IOS_LOCK:
            now = time.monotonic()
            if now - _IOS_WINDOW > 60:
                _IOS_WINDOW = now
                _IOS_COUNT = 0
            if _IOS_COUNT < 150:
                print('[IOS_WEBVIEW_PROBE] ' + stage, flush=True)
                _IOS_COUNT += 1
        self.send_response(204)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_POST(self):
        try:
            path = urlsplit(self.path).path
            if path == '/api/chatpush/delivery-status':
                if not chatpush.callback(urlsplit(self.path).query, self.payload()):
                    raise HTTPFailure(403, 'Неверное подтверждение.')
                return self.send_json(200, {'ok': True})

            mode = self.headers.get('X-App-Mode', 'client')
            user = self.user(mode)
            uid = int(user['id'])
            data = self.payload()
            if not isinstance(data, dict):
                raise HTTPFailure(400, 'Нужен объект JSON.')

            if path == '/api/client/phone-verification/start' and mode == 'client':
                store.upsert_profile(uid, user.get('username', ''), user.get('first_name', ''))
                store.begin_phone_verification(uid)
                return self.send_json(200, {'ok': True, 'expires_in': 180})

            if path == '/api/client/visit-action' and mode == 'client':
                try:
                    result = visits.change(uid, data)
                except crm.BookingError as exc:
                    raise HTTPFailure(exc.status_code, str(exc)) from None
                return self.send_json(200 if result['confirmed'] else 202, result)

            if path == '/api/booking/create' and mode == 'client':
                if not cfg('booking_enabled', True):
                    raise HTTPFailure(503, 'Онлайн-запись отключена.')
                try:
                    original = crm.validate_booking(data)
                    data = dict(data)
                    data['comment'] = booking_comment_with_telegram(original['comment'], user)
                    validated = crm.validate_booking(data)
                    crm.check_date(validated['date'])
                    if validated['date'] == datetime.now(MSK).date().isoformat() and \
                       validated['timeFrom'] <= datetime.now(MSK).strftime('%H:%M'):
                        raise crm.BookingError('Это время уже прошло.', 409, 'time_unavailable')
                    key = data.get('requestKey', '')
                    crm.selection_check(validated['masterID'], validated['serviceID'])
                    cats = crm.catalog()
                    master_name = next((m['name'] for m in cats['masters']
                                        if m['id'] == validated['masterID']), str(validated['masterID']))
                    service_name = next((s['name'] for s in cats['services']
                                         if s['id'] == validated['serviceID']), str(validated['serviceID']))
                    existing = store.ledger_start(uid, key, validated, master_name, service_name)
                    if existing is not None:
                        return self.send_json(200, {'ok': True, **existing, 'replayed': True})
                    try:
                        result = crm.create_booking(data)
                    except crm.BookingError as exc:
                        store.ledger_complete(uid, key,
                            'uncertain' if exc.code == 'uncertain' else 'rejected')
                        raise
                    except Exception:
                        store.ledger_complete(uid, key, 'uncertain')
                        raise crm.BookingError(
                            'Результат неизвестен. Проверьте запись у администратора.', 503, 'uncertain')
                    store.ledger_complete(uid, key, 'done', result)
                    try:
                        notify.enqueue(str(result['visitID']), 'created', validated['date'],
                                       validated['timeFrom'],
                                       validated.get('phoneClient') or data.get('phoneClient'),
                                       uid, master_name, service_name,
                                       enabled=(store.get_setting('auto_enabled', '0') == '1'))
                    except Exception as exc:
                        print('[MiniApp] notify enqueue:', type(exc).__name__, flush=True)
                    return self.send_json(201, {'ok': True, **result})
                except crm.BookingError as exc:
                    raise HTTPFailure(exc.status_code, str(exc))

            if path == '/api/client/review' and mode == 'client':
                raise HTTPFailure(410, 'Отзывы на Яндекс Картах или в 2ГИС.')

            if path == '/api/client/ask' and mode == 'client':
                raise HTTPFailure(410, 'Помощник отключён.')

            if path == '/api/client/support' and mode == 'client':
                message = data.get('message', '')
                if not isinstance(message, str) or not 2 <= len(message.strip()) <= 1200:
                    raise HTTPFailure(400, 'Вопрос 2–1200 символов.')
                with store.db(store.SUPPORT_DB) as con:
                    since = (datetime.now(MSK) - timedelta(hours=1)).isoformat(timespec='seconds')
                    cnt = con.execute('SELECT COUNT(*) FROM tickets WHERE telegram_id=? AND created_at>=?',
                                      (uid, since)).fetchone()[0]
                if cnt >= 5:
                    raise HTTPFailure(429, 'Слишком много обращений.')
                tid = store.create_ticket(uid, 'question', message.strip())
                return self.send_json(200, {'ok': True, 'ticket_id': tid})

            if path == '/api/client/ticket-reply' and mode == 'client':
                try:
                    tid = int(data.get('ticket_id'))
                except (ValueError, TypeError):
                    raise HTTPFailure(400, 'Неверный номер.')
                t = store.ticket(tid)
                if not t or int(t['telegram_id']) != uid:
                    raise HTTPFailure(404, 'Не найдено.')
                if t['status'] == 'closed':
                    raise HTTPFailure(409, 'Обращение закрыто.')
                msg = data.get('message', '')
                if not isinstance(msg, str) or not 1 <= len(msg.strip()) <= 1200:
                    raise HTTPFailure(400, 'Сообщение 1–1200 символов.')
                store.add_ticket_message(tid, 'client', msg.strip())
                return self.send_json(200, {'ok': True})

            if path in ('/api/admin/chatpush/auto', '/api/admin/chatpush/send') and mode == 'admin':
                raise HTTPFailure(409, 'Отправляет внешняя интеграция ChatPush.')

            if path == '/api/admin/chatpush/reconcile' and mode == 'admin':
                vid = str(data.get('visit_id', ''))
                day = str(data.get('day', ''))
                state = str(data.get('state', ''))
                if (not re.fullmatch(r'[0-9]{1,24}', vid)
                        or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day)
                        or state not in ('delivered', 'failed', 'unknown')
                        or data.get('checked_external_chatpush') is not True):
                    raise HTTPFailure(400, 'Проверь историю ChatPush и подтверди.')
                try:
                    live = crm.fetch_day(day)
                except Exception:
                    raise HTTPFailure(503, 'Tetradka недоступна.')
                v = next((x for x in live if str(x.get('id')) == vid), None)
                if not v:
                    raise HTTPFailure(404, 'Визит не найден.')
                chatpush.reconcile_external(vid, day, v['time'], state)
                return self.send_json(200, {'ok': True, 'state': state})

            if path == '/api/admin/mark' and mode == 'admin':
                vid = str(data.get('visit_id', ''))
                day = str(data.get('day', ''))
                state = str(data.get('state', ''))
                if state not in ('pending', 'sms_sent', 'confirmed', 'skipped') \
                        or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
                    raise HTTPFailure(400, 'Неверный статус.')
                if not store.find_visit(vid, day):
                    raise HTTPFailure(404, 'Запись не найдена.')
                store.update_manual_state(vid, day, state)
                if state in ('sms_sent', 'confirmed', 'skipped'):
                    notify.close_manual_tasks(vid, day,
                        {'sms_sent': 'manual_sms_sent', 'confirmed': 'manual_confirmed',
                         'skipped': 'manual_skipped'}[state])
                return self.send_json(200, {'ok': True})

            if path == '/api/admin/reply' and mode == 'admin':
                try:
                    tid = int(data.get('ticket_id'))
                except (ValueError, TypeError):
                    raise HTTPFailure(400, 'Неверный номер.')
                body = data.get('message', '')
                if not isinstance(body, str) or not 1 <= len(body.strip()) <= 1500:
                    raise HTTPFailure(400, 'Ответ 1–1500 символов.')
                t = store.ticket(tid)
                if not t:
                    raise HTTPFailure(404, 'Обращение не найдено.')
                if t['status'] == 'closed':
                    raise HTTPFailure(409, 'Обращение закрыто.')
                store.add_ticket_message(tid, 'admin', body.strip())
                store.set_ticket_status(tid, 'answered')
                return self.send_json(200, {'ok': True})

            if path == '/api/admin/close' and mode == 'admin':
                try:
                    tid = int(data.get('ticket_id'))
                except (ValueError, TypeError):
                    raise HTTPFailure(400, 'Неверный номер.')
                if not store.ticket(tid):
                    raise HTTPFailure(404, 'Не найдено.')
                store.set_ticket_status(tid, 'closed')
                return self.send_json(200, {'ok': True})

            raise HTTPFailure(404, 'Неизвестное действие.')
        except HTTPFailure as ex:
            self.fail(ex)
        except Exception as ex:
            print('[MiniApp] POST error:', type(ex).__name__, flush=True)
            self.fail(ex)


def main():
    store.initialize()
    notify.initialize()
    chatpush.initialize()
    host = os.environ.get('NEFOR_MINIAPP_HOST', '0.0.0.0')
    port = int(os.environ.get('PORT', cfg('miniapp_port', 8787)))
    server = ThreadingHTTPServer((host, port), Server)
    server.daemon_threads = True
    print(f'{SERVER_VERSION}: http://{host}:{port}', flush=True)
    try:
        server.serve_forever(poll_interval=.3)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()