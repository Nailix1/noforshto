"""NEFOR SPOT CLIENT bot."""
from __future__ import annotations
import asyncio, logging, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import storage as store, telegram
from config import cfg

MSK = timezone(timedelta(hours=3))
ROOT = Path(__file__).resolve().parent
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                    handlers=[logging.FileHandler(ROOT / 'client_bot.log', encoding='utf-8'),
                              logging.StreamHandler(sys.stdout)])
log = logging.getLogger('nefor-client')

WELCOME = ('🕸 NEFOR SPOT\n\n'
           'Открой Mini App, чтобы:\n\n'
           '— записаться на стрижку или окрашивание\n'
           '— проверить свои записи\n'
           '— связаться с администратором\n\n'
           'Всё внутри приложения.')


async def api(method, data=None, *, timeout=20):
    token = cfg('client_bot_token')
    if not token:
        raise RuntimeError('Нет client_bot_token')
    r = await asyncio.to_thread(telegram.api_call, token, method, data or {}, timeout)
    if not r.get('ok'):
        raise RuntimeError(f'Telegram {method}: {r.get("description")}')
    return r.get('result')


def kb(rows): return {'inline_keyboard': rows}


def buttons_main():
    url = telegram.app_url(telegram.live_url(), 'client')
    if url:
        return kb([[{'text': '🕸 Открыть Mini App', 'web_app': {'url': url}}]])
    return kb([[{'text': '🕸 Открыть Mini App', 'callback_data': 'openapp'}]])


async def say(chat, text, keyboard=None):
    data = {'chat_id': chat, 'text': text[:3990]}
    if keyboard:
        data['reply_markup'] = keyboard
    return await api('sendMessage', data)


async def notice_admin(ticket_id):
    t = store.ticket(ticket_id)
    if not t:
        return
    icon = '🗓' if t['kind'] in ('booking', 'slots') else '🎞' if t['kind'] == 'media' else '💬'
    lines = [f'{icon} NEFOR SPOT · ОБРАЩЕНИЕ #{ticket_id}',
             f"👤 {t['name']} · TG {t['telegram_id']}"]
    if t['phone']:
        lines.append('📞 +' + t['phone'])
    lines += ['', t['content'][:650]]
    keyboard = kb([[{'text': '💬 Ответить', 'callback_data': f'ticket_reply:{ticket_id}'},
                    {'text': '📋 Обращение', 'callback_data': f'ticket:{ticket_id}'}]])
    try:
        await asyncio.to_thread(telegram.api_call, cfg('bot_token'), 'sendMessage',
            {'chat_id': int(cfg('admin_id')), 'text': '\n'.join(lines), 'reply_markup': keyboard})
    except Exception as ex:
        log.warning('Admin notice failed: %s', ex)


async def welcome(chat):
    url = telegram.app_url(telegram.live_url(), 'client')
    if url:
        try:
            await api('setChatMenuButton', {'chat_id': int(chat),
                'menu_button': {'type': 'web_app', 'text': '🕸 Открыть Mini App',
                                'web_app': {'url': url}}}, timeout=9)
        except Exception as err:
            log.warning('Menu unavailable: %s', type(err).__name__)
    return await say(chat, WELCOME, buttons_main())


async def handle_message(msg):
    user = msg.get('from') or {}
    uid = int(user.get('id', 0))
    if not uid or user.get('is_bot'):
        return
    chat = msg['chat']['id']
    if msg['chat'].get('type') != 'private':
        return
    person = ' '.join(filter(None, [user.get('first_name', ''), user.get('last_name', '')])).strip()
    store.upsert_profile(uid, user.get('username', ''), person)
    txt = str(msg.get('text') or '').strip()
    if txt.startswith('/start') or txt in ('🏠 Меню', '↩️ Главное меню', '/menu', '/refresh'):
        store.reset_session(uid)
        return await welcome(chat)
    if txt == '/cancel':
        store.reset_session(uid)
        return await welcome(chat)
    contact = msg.get('contact')
    if contact:
        store.accept_contact(uid, contact, msg.get('date'))
        return
    if txt:
        tid = store.create_ticket(uid, 'question', txt)
        store.set_session(uid, mode='support', active_ticket=str(tid))
        await notice_admin(tid)
        await say(chat,
                  f'✅ Сообщение #{tid} передано администратору. '
                  f'Ответ смотри в разделе «Помощь» Mini App.',
                  buttons_main())


async def handle_callback(cb):
    uid = int(cb.get('from', {}).get('id', 0))
    if not uid:
        return
    msg = cb.get('message') or {}
    chat = msg.get('chat', {}).get('id', uid)
    key = str(cb.get('data') or '')
    try:
        await api('answerCallbackQuery', {'callback_query_id': cb['id']}, timeout=8)
    except Exception:
        pass
    if key == 'openapp':
        url = telegram.app_url(telegram.live_url(), 'client')
        if url:
            return await say(chat, 'Открой Mini App 👇',
                             kb([[{'text': '🕸 Открыть Mini App', 'web_app': {'url': url}}]]))
        return await say(chat, '⚠️ HTTPS-ссылка Mini App не настроена.')
    if key == 'home':
        store.reset_session(uid)
        return await welcome(chat)


async def handle(update):
    if update.get('message'):
        return await handle_message(update['message'])
    if update.get('callback_query'):
        return await handle_callback(update['callback_query'])


async def main():
    store.initialize()
    me = await api('getMe')
    log.info('NEFOR SPOT CLIENT: @%s', me.get('username'))
    try:
        url = telegram.app_url(telegram.live_url(), 'client')
        if url:
            await asyncio.to_thread(telegram.set_menu, cfg('client_bot_token'), url)
    except Exception as exc:
        log.warning('Menu: %s', exc)
    try:
        await api('deleteWebhook', {'drop_pending_updates': False})
    except Exception as ex:
        log.warning('deleteWebhook: %s', ex)
    offset = 0
    while True:
        try:
            updates = await api('getUpdates',
                {'offset': offset, 'timeout': 12,
                 'allowed_updates': ['message', 'callback_query']}, timeout=19)
            for u in updates:
                offset = max(offset, u['update_id'] + 1)
                try:
                    await handle(u)
                except Exception as e:
                    log.exception('Update failed: %s', e)
        except Exception as e:
            log.warning('Reconnect: %s', e)
            await asyncio.sleep(3)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print('Client bot stopped')
    except Exception as ex:
        log.exception('Fatal: %s', ex)
        sys.exit(1)