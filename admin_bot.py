"""NEFOR SPOT ADMIN bot."""
from __future__ import annotations
import asyncio, logging, os, re, sys
from datetime import datetime, timedelta, timezone

import crm, storage as store, notify, chatpush, telegram
from config import cfg

MSK = timezone(timedelta(hours=3))
ADDRESS = 'Разъезжая 42/34, вход в арку рядом с КБ, домофон 55'
RELEASE = 'NEFOR SPOT 9.5'

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('nefor-admin')

CLIENT_CACHE = {'at': 0.0, 'data': None}
REPLY_WAITING = {}


def inline(rows): return {'inline_keyboard': rows}


# ─── Telegram API ──────────────────────────────────────────────────────────

async def tg(method: str, data=None, *, retries=2, timeout=18):
    token = cfg('bot_token')
    if not token:
        raise RuntimeError('Нет bot_token')
    for n in range(retries):
        try:
            r = await asyncio.to_thread(telegram.api_call, token, method, data or {}, timeout)
            if not r.get('ok'):
                raise RuntimeError(f'Telegram {method}: {str(r.get("description", r))[:180]}')
            return r['result']
        except (OSError, asyncio.TimeoutError):
            if n == retries - 1:
                raise
            await asyncio.sleep(2)


async def send(chat_id, text, keyboard=None):
    args = {'chat_id': chat_id, 'text': text[:3990]}
    if keyboard:
        args['reply_markup'] = keyboard
    return await tg('sendMessage', args)


async def edit(chat_id, message_id, text, keyboard=None):
    if message_id is None:
        return await send(chat_id, text, keyboard)
    args = {'chat_id': chat_id, 'message_id': message_id, 'text': text[:3990]}
    if keyboard:
        args['reply_markup'] = keyboard
    try:
        return await tg('editMessageText', args)
    except RuntimeError as exc:
        if 'message is not modified' in str(exc):
            return None
        return await send(chat_id, text, keyboard)


async def send_via_client_bot(uid: int, text: str) -> bool:
    """Отправка сообщения клиенту через client_bot_token.
    Возвращает True, если Telegram подтвердил отправку."""
    token = cfg('client_bot_token')
    if not token:
        log.warning('client_bot_token не задан — отправка клиенту невозможна')
        return False
    try:
        r = await asyncio.to_thread(telegram.api_call, token, 'sendMessage',
                                    {'chat_id': int(uid), 'text': text[:3990]}, 15)
        return bool(r.get('ok'))
    except Exception as e:
        log.warning('send_via_client_bot failed: %s', type(e).__name__)
        return False


# ─── Нормализация визитов ──────────────────────────────────────────────────

def normalize_visit(v: dict, day: str = '') -> dict:
    if not isinstance(v, dict):
        return {}
    out = dict(v)
    if 'visit_id' not in out and 'id' in out:
        out['visit_id'] = out['id']
    if 'day' not in out and day:
        out['day'] = day
    if 'at_time' not in out and 'time' in out:
        out['at_time'] = out['time']
    if 'notifications_disabled' not in out:
        out['notifications_disabled'] = 0
    if 'manual_state' not in out:
        out['manual_state'] = 'pending'
    return out


def normalize_rows(rows, day: str = '') -> list:
    return [normalize_visit(v, day) for v in (rows or []) if isinstance(v, dict)]


# ─── Обогащение и статусы ──────────────────────────────────────────────────

def with_delivery(rows):
    rows = normalize_rows(rows)
    found = notify.delivery_for_visits(rows)
    external = chatpush.for_visits(rows)
    external_checked = chatpush.external_for_visits(rows)
    web_history = chatpush.history_for_visits(rows) if chatpush.history_enabled() else {}
    result = []
    for item in rows:
        v = dict(item)
        report = found.get((str(v['visit_id']), str(v['day'])))
        if v['manual_state'] == 'confirmed':
            state = 'confirmed'
        elif v['manual_state'] == 'sms_sent':
            state = 'sms_sent'
        elif v['manual_state'] == 'skipped':
            state = 'skipped'
        elif v['notifications_disabled']:
            state = 'disabled'
        elif report:
            state = {'sent': 'telegram_sent', 'manual': 'manual_sms', 'needs_review': 'check',
                     'queued': 'pending', 'attempting': 'check',
                     'manual_sms_sent': 'sms_sent', 'manual_confirmed': 'confirmed',
                     'manual_skipped': 'skipped'}.get(report['state'], 'check')
        else:
            state = 'pending'
        if state == 'telegram_sent':
            notice_state, notice_channel, notice_source = 'notified', 'telegram', 'nefor'
        elif state == 'sms_sent':
            notice_state, notice_channel, notice_source = 'notified', 'sms', 'admin_manual'
        elif state == 'confirmed':
            notice_state, notice_channel, notice_source = 'confirmed', 'manual', 'admin_manual'
        elif state == 'disabled':
            notice_state, notice_channel, notice_source = 'disabled', '', 'tetradka'
        elif state == 'skipped':
            notice_state, notice_channel, notice_source = 'skipped', '', 'admin_manual'
        else:
            notice_state, notice_channel, notice_source = 'unverified', '', 'none'

        cp_row = external.get((str(v['visit_id']), str(v['day'])))
        if cp_row:
            if cp_row['state'] in ('delivered', 'read'):
                notice_state, notice_channel, notice_source = 'notified', 'telegram', 'chatpush'
            elif cp_row['state'] in ('accepted', 'attempting', 'uncertain'):
                if notice_state not in ('notified', 'confirmed'):
                    notice_state, notice_source = 'check', 'chatpush'

        audit = external_checked.get((str(v['visit_id']), str(v['day']), str(v['at_time'])))
        if audit and notice_state not in ('confirmed', 'disabled', 'skipped'):
            if audit['state'] == 'delivered':
                notice_state, notice_channel, notice_source = 'notified', 'telegram', 'chatpush_admin_verified'
            elif audit['state'] == 'failed':
                notice_state, notice_source = 'unverified', 'chatpush_admin_verified'
            elif audit['state'] == 'unknown':
                notice_state, notice_source = 'check', 'chatpush_admin_verified'

        history = web_history.get(store.norm_phone(v.get('phone') or ''), {})
        if (history and not cp_row and not audit
                and v['manual_state'] not in ('sms_sent', 'confirmed', 'skipped')
                and not v['notifications_disabled']
                and notice_state not in ('notified', 'confirmed')):
            hs = history.get('state')
            if hs == 'telegram':
                notice_state, notice_channel, notice_source = 'notified', 'telegram', 'chatpush_external_history'
            elif hs == 'no_telegram':
                notice_state, notice_source = 'unverified', 'chatpush_external_history'
            elif hs in ('pending', 'unknown'):
                notice_state, notice_source = 'check', 'chatpush_external_history'

        v.update(notice_state=notice_state, notice_channel=notice_channel,
                 notice_source=notice_source, delivery_status=state,
                 chatpush_status=cp_row['state'] if cp_row else 'untracked',
                 chatpush_delivery=cp_row or {},
                 chatpush_external_audit=audit or {},
                 chatpush_external_history=history)
        result.append(v)
    return result


def admin_notice(v):
    cp = v.get('chatpush_delivery') or {}
    history = v.get('chatpush_external_history') or {}
    state = v.get('notice_state') or ''
    if v.get('manual_state') == 'sms_sent' or (state == 'notified' and v.get('notice_channel') == 'sms'):
        return {'bucket': 'notified', 'label': 'SMS · отмечено вручную', 'klass': 'ok',
                'source': 'Отметка администратора'}
    if v.get('manual_state') == 'confirmed' or state == 'confirmed':
        return {'bucket': 'notified', 'label': 'Клиент подтвердил', 'klass': 'ok',
                'source': 'Отметка администратора'}
    if cp.get('state') in ('delivered', 'read'):
        return {'bucket': 'notified',
                'label': 'TG · прочитано · ChatPush' if cp['state'] == 'read' else 'TG · доставлено · ChatPush',
                'klass': 'ok', 'source': 'Подтверждённый ответ ChatPush'}
    if state == 'notified':
        src_map = {
            'chatpush_external_history': ('TG · история ChatPush',
                'Подтверждена последняя Telegram-доставка по номеру'),
            'chatpush_admin_verified': ('TG · доставлено · ChatPush (сверено)',
                'Проверено администратором по истории ChatPush')}
        label, source = src_map.get(v.get('notice_source'), ('TG · доставлено', 'Журнал доставки'))
        return {'bucket': 'notified', 'label': label, 'klass': 'ok', 'source': source}
    if v.get('notice_source') == 'chatpush_external_history':
        if history.get('state') == 'no_telegram':
            return {'bucket': 'sms', 'label': 'Telegram не найден · ChatPush', 'klass': 'warn',
                    'source': 'Последний подтверждённый ответ Telegram'}
        if history.get('state') == 'pending':
            return {'bucket': 'check', 'label': 'ChatPush · в очереди', 'klass': 'warn',
                    'source': 'Не отправлять SMS до результата'}
        if history.get('state') == 'telegram':
            return {'bucket': 'notified', 'label': 'Telegram · ChatPush', 'klass': 'ok',
                    'source': 'Последнее Telegram-сообщение'}
    if v.get('manual_state') == 'skipped' or state == 'skipped':
        return {'bucket': 'other', 'label': 'Пропущено вручную', 'klass': '',
                'source': 'Отметка администратора'}
    if state == 'disabled' or v.get('notifications_disabled'):
        return {'bucket': 'other', 'label': 'Уведомления отключены', 'klass': '', 'source': 'Тетрадка'}
    if cp.get('state') == 'accepted':
        return {'bucket': 'check', 'label': 'ChatPush · в очереди', 'klass': 'warn',
                'source': 'Ожидается финальный статус'}
    if cp.get('state') in ('attempting', 'uncertain'):
        return {'bucket': 'check', 'label': 'ChatPush · результат неясен', 'klass': 'warn',
                'source': 'Требуется ручная проверка'}
    if cp.get('state') in ('failed', 'rejected'):
        return {'bucket': 'sms', 'label': 'ChatPush · ошибка Telegram', 'klass': 'warn',
                'source': 'Не доставлено, нужна ручная связь'}
    if v.get('notice_source') == 'chatpush_admin_verified' and state == 'unverified':
        return {'bucket': 'sms', 'label': 'ChatPush · не доставлено (сверено)', 'klass': 'warn',
                'source': 'Проверено администратором'}
    if state == 'check':
        return {'bucket': 'check', 'label': 'Сверить историю ChatPush', 'klass': 'warn',
                'source': 'Неизвестный результат ≠ нужна SMS'}
    if not v.get('phone'):
        return {'bucket': 'other', 'label': 'Нет номера', 'klass': 'warn', 'source': 'Тетрадка'}
    return {'bucket': 'check', 'label': 'ChatPush не сверен', 'klass': 'warn',
            'source': 'Нет доказательств, что нужна SMS'}


def sms_text(v):
    d = datetime.fromisoformat(v['day'])
    tomorrow = (datetime.now(MSK) + timedelta(days=1)).date().isoformat() == v['day']
    when = f'завтра, {d:%d.%m}' if tomorrow else f'{d:%d.%m}'
    return f"Здравствуйте! Это NEFOR SPOT. Вы записаны {when}, в {v['at_time']}.\n\nВСЁ В СИЛЕ?\n\nАдрес: {ADDRESS}."


# ─── Напоминания ───────────────────────────────────────────────────────────

def reminder_text(v: dict) -> str:
    d = datetime.fromisoformat(v['day'])
    now = datetime.now(MSK)
    day_diff = (d.date() - now.date()).days
    if day_diff == 0:
        when = f'сегодня в {v["at_time"]}'
    elif day_diff == 1:
        when = f'завтра в {v["at_time"]}'
    elif day_diff == 2:
        when = f'послезавтра в {v["at_time"]}'
    else:
        when = f'{d:%d.%m} в {v["at_time"]}'
    return (
        '🔔 Напоминаем о вашей записи в NEFOR SPOT\n\n'
        f'Когда: {when}\n'
        f'Мастер: {v.get("master") or "—"}\n'
        f'Услуга: {v.get("service") or "—"}\n\n'
        f'Адрес: {ADDRESS}\n\n'
        'Если не сможете прийти — напишите администратору заранее.'
    )


def find_client_uid(v: dict):
    """Кому отправить напоминание: сначала точное совпадение по visit_id
    (это точно тот, кто записался через Mini App), потом по телефону."""
    try:
        uid = store.ledger_telegram_for_visit(int(v.get('visit_id')))
        if uid:
            return uid
    except (TypeError, ValueError):
        pass
    phone = store.norm_phone(v.get('phone') or '')
    if phone:
        return notify.linked_telegram_id(phone)
    return None


async def deliver_reminder(v: dict, *, kind: str = 'manual') -> tuple[bool, str]:
    uid = find_client_uid(v)
    if not uid:
        return False, 'Не найден Telegram-получатель. Клиент не открывал Mini App и не подтвердил номер.'
    if store.reminder_already_sent(v['visit_id'], v['day'], v['at_time']):
        return False, 'Напоминание по этому визиту уже отправлено ранее.'
    text = reminder_text(v)
    ok = await send_via_client_bot(uid, text)
    if not ok:
        return False, 'Telegram отклонил отправку. Возможно, клиент заблокировал бота.'
    store.reminder_mark(v['visit_id'], v['day'], v['at_time'], uid, kind)
    return True, 'Напоминание отправлено клиенту.'


async def auto_reminder_loop():
    """Раз в 15 минут проверяет записи на сегодня и завтра и отправляет
    напоминание клиенту за 3–12 часов до начала визита.
    Дубли исключены таблицей reminders."""
    await asyncio.sleep(45)
    while True:
        try:
            if store.get_setting('auto_reminders', '1') != '1':
                await asyncio.sleep(15 * 60)
                continue
            now = datetime.now(MSK)
            days = [
                now.date().isoformat(),
                (now.date() + timedelta(days=1)).isoformat(),
            ]
            visits = []
            for day in days:
                try:
                    rows = await asyncio.to_thread(crm.fetch_day, day)
                    store.save_visits(day, rows)
                    visits.extend(store.rows_for_date(day))
                except Exception as e:
                    log.warning('auto_reminder fetch %s: %s', day, type(e).__name__)
            for v in visits:
                try:
                    when = datetime.fromisoformat(v['day'] + 'T' + v['at_time']).replace(tzinfo=MSK)
                except Exception:
                    continue
                hours = (when - now).total_seconds() / 3600.0
                if not (3.0 <= hours <= 12.0):
                    continue
                if store.reminder_already_sent(v['visit_id'], v['day'], v['at_time']):
                    continue
                ok, reason = await deliver_reminder(v, kind='auto')
                log.info('auto_reminder visit=%s ok=%s reason=%s', v['visit_id'], ok, reason)
                await asyncio.sleep(2)
        except Exception as e:
            log.exception('auto_reminder_loop error: %s', type(e).__name__)
        await asyncio.sleep(15 * 60)


# ─── Экраны ────────────────────────────────────────────────────────────────

def admin_menu():
    url = telegram.app_url(telegram.live_url(), 'admin')
    rows = [[{'text': '📅 Завтра'}, {'text': '📱 SMS вручную'}, {'text': '⚠️ Сверить ChatPush'}],
            [{'text': '👥 База клиентов'}, {'text': '💬 Обращения'},
             {'text': '⚙️ Система'}]]
    if url:
        rows.insert(0, [{'text': '🩷 Открыть приложение', 'web_app': {'url': url}}])
    return {'keyboard': rows, 'resize_keyboard': True, 'is_persistent': True}


async def fetch_tomorrow_rows(day: str):
    try:
        items = await asyncio.to_thread(crm.fetch_day, day)
        store.save_visits(day, items)
        return store.rows_for_date(day), True
    except Exception as e:
        log.warning('fetch_tomorrow_rows failed: %s', type(e).__name__)
        return store.rows_for_date(day), False


async def screen_tomorrow(chat, msg=None):
    day = crm.appointment_day()
    rows, fresh = await fetch_tomorrow_rows(day)
    try:
        enriched = with_delivery(rows)
    except Exception as e:
        log.exception('with_delivery failed in screen_tomorrow')
        return await edit(chat, msg,
            f'⚠️ Ошибка при разборе визитов: {type(e).__name__}: {str(e)[:200]}',
            inline([[{'text': '🔄 Повторить', 'callback_data': 'tomorrow'}]]))
    count = {'all': len(enriched), 'notified': 0, 'sms': 0, 'check': 0, 'other': 0}
    for v in enriched:
        b = admin_notice(v)['bucket']
        if b in count:
            count[b] += 1
    auto_on = store.get_setting('auto_reminders', '1') == '1'
    summary = (f'📅 Завтра · {day}\nВсего: {count["all"]} · '
               f'Уведомлены: {count["notified"]} · Требуют SMS: {count["sms"]} · '
               f'Сверить: {count["check"]}')
    lines = [summary, f'🔔 Автонапоминания: {"включены" if auto_on else "выключены"}', '']
    for v in enriched[:25]:
        info = admin_notice(v)
        lines.append(f"{v['at_time']} · {v['name']} · {v['master']} — {info['label']}")
    if not enriched:
        lines.append('Завтра записей нет.' if fresh else 'Расписание не загружено.')
    kb = []
    for v in enriched[:20]:
        info = admin_notice(v)
        if v.get('phone'):
            kb.append([
                {'text': f"📋 SMS · {v['at_time']} {v['name'][:18]}",
                 'copy_text': {'text': sms_text(v)}},
                {'text': '📞 Номер', 'copy_text': {'text': '+' + v['phone']}}])
        if info['bucket'] in ('sms', 'check'):
            kb.append([{'text': f"✓ {v['at_time']} {v['name'][:20]}",
                        'callback_data': f"visit:{v['visit_id']}"}])
    kb.append([{'text': '📱 SMS вручную', 'callback_data': 'manual_sms'},
               {'text': '⚠️ Сверить ChatPush', 'callback_data': 'chatpush_check'}])
    kb.append([{'text': f'🔔 Авто: {"вкл" if auto_on else "выкл"}', 'callback_data': 'remind_toggle'}])
    kb.append([{'text': '🔄 Обновить', 'callback_data': 'tomorrow'},
               {'text': '⚙️ Система', 'callback_data': 'system'}])
    return await edit(chat, msg, '\n'.join(lines)[:3900], inline(kb))


async def screen_manual_sms(chat, msg=None):
    day = crm.appointment_day()
    try:
        rows, _ = await fetch_tomorrow_rows(day)
    except Exception:
        return await edit(chat, msg,
            '⚠️ Расписание не загружено. Не отправляй SMS по сохранённым данным.',
            inline([[{'text': '🔄 Повторить', 'callback_data': 'manual_sms'},
                     {'text': '📅 Назад', 'callback_data': 'tomorrow'}]]))
    try:
        enriched = with_delivery(rows)
    except Exception as e:
        log.exception('with_delivery failed in screen_manual_sms')
        return await edit(chat, msg,
            f'⚠️ Ошибка: {type(e).__name__}: {str(e)[:200]}',
            inline([[{'text': '🔄 Повторить', 'callback_data': 'manual_sms'}]]))
    manual = [v for v in enriched if admin_notice(v)['bucket'] == 'sms']
    lines = [f'📱 SMS ВРУЧНУЮ · {day}', f'Подтверждённых задач: {len(manual)}', '']
    for v in manual[:30]:
        lines.append(f"🔴 {v['at_time']} · {v['name']} · {v['master']} · +{v['phone']}")
    if not manual:
        lines.append('Нет задач для ручной связи.')
    kb = [[{'text': f"{v['at_time']} {v['name'][:22]}", 'callback_data': f"visit:{v['visit_id']}"}]
          for v in manual[:25]]
    kb.append([{'text': '🔄 Обновить', 'callback_data': 'manual_sms'},
               {'text': '📅 Все', 'callback_data': 'tomorrow'}])
    return await edit(chat, msg, '\n'.join(lines)[:3900], inline(kb))


async def screen_chatpush_check(chat, msg=None):
    day = crm.appointment_day()
    try:
        rows, _ = await fetch_tomorrow_rows(day)
    except Exception:
        return await edit(chat, msg, '⚠️ Нет свежих визитов Тетрадки.',
                          inline([[{'text': '🔄 Повторить', 'callback_data': 'chatpush_check'}]]))
    try:
        enriched = with_delivery(rows)
    except Exception as e:
        log.exception('with_delivery failed in screen_chatpush_check')
        return await edit(chat, msg,
            f'⚠️ Ошибка: {type(e).__name__}: {str(e)[:200]}',
            inline([[{'text': '🔄 Повторить', 'callback_data': 'chatpush_check'}]]))
    check = [v for v in enriched if admin_notice(v)['bucket'] == 'check']
    lines = [f'⚠️ СВЕРКА CHATPUSH · {day}', f'Нет подтверждённого результата: {len(check)}', '']
    for v in check[:30]:
        lines.append(f"{v['at_time']} · {v['name']} · {v['master']}")
    if not check:
        lines.append('Неизвестных исходов нет.')
    kb = [[{'text': f"{v['at_time']} {v['name'][:22]}", 'callback_data': f"visit:{v['visit_id']}"}]
          for v in check[:25]]
    kb.append([{'text': '📱 SMS вручную', 'callback_data': 'manual_sms'},
               {'text': '🔄 Обновить', 'callback_data': 'chatpush_check'}])
    return await edit(chat, msg, '\n'.join(lines)[:3800], inline(kb))


def visit_keyboard(v):
    phone = v['phone']
    kb = []
    info = admin_notice(v)
    # Ручное напоминание через клиентский бот
    can_remind = bool(find_client_uid(v))
    if can_remind:
        kb.append([{'text': '🔔 Отправить напоминание в бот',
                    'callback_data': f"remind:{v['visit_id']}:{v['day']}"}])
    if phone and info['bucket'] == 'sms':
        kb.append([{'text': '📋 Скопировать номер', 'copy_text': {'text': '+' + phone}}])
        kb.append([{'text': '📋 Скопировать текст SMS', 'copy_text': {'text': sms_text(v)}}])
        kb.append([{'text': '📱 SMS отправлена вручную', 'callback_data': f"mark_sms:{v['visit_id']}"}])
    kb.append([{'text': '✅ Клиент подтвердил', 'callback_data': f"mark_confirm:{v['visit_id']}"}])
    kb.append([{'text': '⏭ Пропустить', 'callback_data': f"mark_skip:{v['visit_id']}"},
               {'text': '↩️ Сбросить', 'callback_data': f"mark_pending:{v['visit_id']}"}])
    if info['bucket'] == 'check':
        kb.append([{'text': '⚠️ Сверил: доставлено', 'callback_data': f"cp_mark_delivered:{v['visit_id']}"}])
        kb.append([{'text': '📱 Сверил: не доставлено', 'callback_data': f"cp_mark_failed:{v['visit_id']}"}])
    kb.append([{'text': '📅 Назад', 'callback_data': 'tomorrow'}])
    return inline(kb)


async def screen_visit(chat, msg, visit_id):
    v = store.find_visit_any(visit_id)
    if not v:
        return await edit(chat, msg, 'Запись не найдена.',
                          inline([[{'text': '📅 Завтра', 'callback_data': 'tomorrow'}]]))
    v = with_delivery([v])[0]
    info = admin_notice(v)
    client_uid = find_client_uid(v)
    reminder_sent = store.reminder_already_sent(v['visit_id'], v['day'], v['at_time'])
    lines = [f"📋 {v['name']}", f"🗓 {v['day']} · {v['at_time']}",
             f"✂️ {v['master']}", f"🧴 {v['service']}",
             f"📞 +{v['phone'] if v['phone'] else '—'}", '',
             f"Статус: {info['label']}", f"Источник: {info['source']}"]
    if client_uid:
        lines.append(f"🔔 Напоминание: {'отправлено' if reminder_sent else 'можно отправить'}")
    else:
        lines.append('🔔 Напоминание: клиент не открывал Mini App, отправка невозможна')
    if v['phone'] and info['bucket'] == 'sms':
        lines += ['', 'Текст SMS:', sms_text(v)]
    return await edit(chat, msg, '\n'.join(lines), visit_keyboard(v))


async def screen_system(chat, msg=None):
    auto = store.get_setting('auto_reminders', '1') == '1'
    auto_legacy = store.get_setting('auto_enabled', '0') == '1'
    body = (
        f'⚙️ Система · {RELEASE}\n\n'
        f'🟢 Telegram-бот · запущен\n'
        f'{"🟢" if crm.crm_configured() else "⚪"} Tetradka CRM\n'
        f'{"🟢" if chatpush.configured() else "⚪"} ChatPush API\n\n'
        f'🔔 Автонапоминания за 3–12 ч: {"ВКЛЮЧЕНЫ" if auto else "выключены"}\n'
        f'Канал напоминаний: клиентский бот @zalupaspotbot\n\n'
        f'Уведомления о создании/переносе: внешний ChatPush.\n'
        f'Ручные SMS: отдельный раздел.'
    )
    kb = inline([
        [{'text': f'{"🔕 Выключить" if auto else "🔔 Включить"} автонапоминания',
          'callback_data': 'remind_toggle'}],
        [{'text': '📱 SMS вручную', 'callback_data': 'manual_sms'}],
        [{'text': '🔄 Обновить', 'callback_data': 'system'}],
    ])
    return await edit(chat, msg, body, kb)


async def screen_clients(chat, msg=None):
    try:
        clients = await asyncio.to_thread(crm.fetch_clients)
    except Exception as ex:
        return await edit(chat, msg, f'👥 База\n\n⚠️ {ex}',
                          inline([[{'text': '🔄', 'callback_data': 'clients'}]]))
    CLIENT_CACHE['data'] = clients
    CLIENT_CACHE['at'] = datetime.now(MSK).timestamp()
    now = datetime.now(MSK).date()
    counts = {'active': 0, 'sleep': 0, 'lost': 0}
    for c in clients:
        raw = str(c.get('lastVisit') or '')[:10]
        try:
            days = (now - datetime.fromisoformat(raw).date()).days
        except ValueError:
            counts['lost'] += 1
            continue
        counts['active' if days <= 92 else 'sleep' if days <= 183 else 'lost'] += 1
    kb = inline([
        [{'text': f'🟢 Постоянные · {counts["active"]}', 'callback_data': 'cllist:active:0'}],
        [{'text': f'🟡 Спящие · {counts["sleep"]}', 'callback_data': 'cllist:sleep:0'}],
        [{'text': f'⚫ Пропавшие · {counts["lost"]}', 'callback_data': 'cllist:lost:0'}],
        [{'text': f'👥 Все · {len(clients)}', 'callback_data': 'cllist:all:0'}],
        [{'text': '🔄 Обновить', 'callback_data': 'cl_refresh'}]])
    return await edit(chat, msg, f'👥 База клиентов\n\nВсего: {len(clients)}', kb)


def _segment(c):
    raw = str(c.get('lastVisit') or '')[:10]
    try:
        days = (datetime.now(MSK).date() - datetime.fromisoformat(raw).date()).days
    except ValueError:
        return 'lost'
    return 'active' if days <= 92 else 'sleep' if days <= 183 else 'lost'


def _cid(c): return str(c.get('clientID') or c.get('id') or '')
def _cname(c): return str(c.get('name') or 'Без имени')


async def screen_client_list(chat, msg, segment, page):
    clients = CLIENT_CACHE['data'] or []
    arr = [c for c in clients if segment == 'all' or _segment(c) == segment]
    arr.sort(key=lambda c: _cname(c).casefold())
    page = max(0, int(page))
    pages = max(1, (len(arr) + 7) // 8)
    page = min(page, pages - 1)
    chunk = arr[page * 8:page * 8 + 8]
    kb = [[{'text': f'👤 {_cname(c)[:35]}', 'callback_data': f'client:{_cid(c)}'}] for c in chunk]
    nav = []
    if page:
        nav.append({'text': '◀️', 'callback_data': f'cllist:{segment}:{page-1}'})
    if page + 1 < pages:
        nav.append({'text': '▶️', 'callback_data': f'cllist:{segment}:{page+1}'})
    if nav:
        kb.append(nav)
    kb.append([{'text': '⬅️ База', 'callback_data': 'clients'}])
    names = {'active': 'Постоянные', 'sleep': 'Спящие', 'lost': 'Пропавшие', 'all': 'Все'}
    return await edit(chat, msg, f'👥 {names[segment]} · {len(arr)}\nСтр. {page+1}/{pages}', inline(kb))


async def screen_client(chat, msg, cid):
    c = next((r for r in (CLIENT_CACHE['data'] or []) if _cid(r) == cid), None)
    if not c:
        return await edit(chat, msg, 'Клиент не найден.',
                          inline([[{'text': '⬅️ База', 'callback_data': 'clients'}]]))
    phone = store.norm_phone(c.get('phone'))
    last = c.get('lastService') or {}
    body = (f"👤 {_cname(c)}\n\n📞 +{phone or '—'}\n"
            f"Последний визит: {str(c.get('lastVisit') or '—')[:10]}\n"
            f"Мастер: {(last.get('masterName') or '—') if isinstance(last, dict) else '—'}\n"
            f"Услуга: {(last.get('serviceName') or '—') if isinstance(last, dict) else '—'}")
    return await edit(chat, msg, body, inline([[{'text': '⬅️ База', 'callback_data': 'clients'}]]))


async def screen_tickets(chat, msg=None):
    data = store.tickets('new', 15)
    lines = [f'💬 Обращения · новых: {len(data)}']
    rows = []
    for t in data:
        icon = '🗓' if t['kind'] in ('booking', 'slots') else '⭐' if t['kind'] == 'review' \
            else '🎞' if t['kind'] == 'media' else '💬'
        rows.append([{'text': f"{icon} #{t['id']} · {t['name'][:27]}", 'callback_data': f"ticket:{t['id']}"}])
    rows.append([{'text': '🔄', 'callback_data': 'tickets'}, {'text': '📅', 'callback_data': 'tomorrow'}])
    return await edit(chat, msg, '\n'.join(lines), inline(rows))


async def screen_ticket(chat, msg, ticket_id):
    t = store.ticket(ticket_id)
    if not t:
        return await edit(chat, msg, 'Обращение не найдено.',
                          inline([[{'text': '↩️', 'callback_data': 'tickets'}]]))
    lines = [f"💬 #{t['id']} · {t['status']}", f"👤 {t['name']}"]
    if t['phone']:
        lines.append('📞 +' + t['phone'])
    lines += ['', t['content'][:2200]]
    rows = [[{'text': '💬 Ответить', 'callback_data': f"ticket_reply:{t['id']}"}]]
    if t['status'] != 'closed':
        rows.append([{'text': '✅ Закрыть', 'callback_data': f"ticket_close:{t['id']}"}])
    rows.append([{'text': '↩️', 'callback_data': 'tickets'}])
    return await edit(chat, msg, '\n'.join(lines), inline(rows))


# ─── Обработчики ───────────────────────────────────────────────────────────

async def handle_message(msg):
    sender = int(msg.get('from', {}).get('id', 0))
    admin_id_val = int(cfg('admin_id') or 0)
    chat = msg['chat']['id']
    txt = str(msg.get('text') or '')
    if sender != admin_id_val:
        if msg.get('chat', {}).get('type') == 'private' \
                and txt.strip().split(' ', 1)[0] in ('/start', '/id', '/whoami'):
            await send(chat, f'⛔ Доступ не выдан.\nТвой Telegram ID: {sender}')
        return
    if txt.startswith('/start') or txt in ('/id', '/whoami'):
        await send(chat, f'{RELEASE}\n\nTelegram ID: {sender}\n'
                         f'CRM: {"подключена" if crm.crm_configured() else "не подключена"}', admin_menu())
        if crm.crm_configured():
            return await screen_tomorrow(chat)
        return
    if txt in ('📅 Завтра', '/tomorrow'):
        try:
            return await screen_tomorrow(chat)
        except Exception as e:
            log.exception('screen_tomorrow failed')
            return await send(chat, f'⚠️ Не удалось открыть расписание.\n\n{type(e).__name__}: {str(e)[:200]}')
    if txt in ('📱 SMS вручную', '/sms'):
        try:
            return await screen_manual_sms(chat)
        except Exception as e:
            log.exception('screen_manual_sms failed')
            return await send(chat, f'⚠️ Ошибка SMS.\n\n{type(e).__name__}: {str(e)[:200]}')
    if txt in ('⚠️ Сверить ChatPush', '/chatpush'):
        try:
            return await screen_chatpush_check(chat)
        except Exception as e:
            log.exception('screen_chatpush_check failed')
            return await send(chat, f'⚠️ Ошибка сверки.\n\n{type(e).__name__}: {str(e)[:200]}')
    if txt in ('👥 База клиентов', '/clients'):
        return await screen_clients(chat)
    if txt in ('⚙️ Система', '/system'):
        return await screen_system(chat)
    if txt in ('💬 Обращения', '/tickets'):
        return await screen_tickets(chat)
    if chat in REPLY_WAITING:
        if txt == '/cancel':
            REPLY_WAITING.pop(chat, None)
            return await send(chat, 'Отменено.', admin_menu())
        tid = REPLY_WAITING[chat]
        t = store.ticket(tid)
        if not t:
            REPLY_WAITING.pop(chat, None)
            return await send(chat, 'Обращение не найдено.')
        if not txt:
            return await send(chat, 'Пришли текст или /cancel.')
        store.add_ticket_message(tid, 'admin', txt)
        store.set_ticket_status(tid, 'answered')
        REPLY_WAITING.pop(chat, None)
        return await send(chat, f'✅ Ответ сохранён в Mini App по #{tid}.', admin_menu())
    await send(chat, 'Используй кнопки меню.', admin_menu())


async def handle_callback(cb):
    if int(cb.get('from', {}).get('id', 0)) != int(cfg('admin_id') or 0):
        return
    try:
        await tg('answerCallbackQuery', {'callback_query_id': cb['id']}, retries=1, timeout=8)
    except Exception:
        pass
    chat = cb['message']['chat']['id']
    mid = cb['message']['message_id']
    key = cb.get('data', '')

    try:
        if key in ('tomorrow', 'home'):
            return await screen_tomorrow(chat, mid)
        if key == 'manual_sms':
            return await screen_manual_sms(chat, mid)
        if key == 'chatpush_check':
            return await screen_chatpush_check(chat, mid)
        if key == 'system':
            return await screen_system(chat, mid)
        if key == 'remind_toggle':
            current = store.get_setting('auto_reminders', '1')
            new = '0' if current == '1' else '1'
            store.set_setting('auto_reminders', new)
            return await screen_system(chat, mid)
        if key.startswith('remind:'):
            parts = key.split(':', 2)
            if len(parts) < 2:
                return
            vid = parts[1]
            v = store.find_visit_any(vid)
            if not v:
                return await edit(chat, mid, 'Запись не найдена.',
                                  inline([[{'text': '📅 Завтра', 'callback_data': 'tomorrow'}]]))
            ok, reason = await deliver_reminder(v, kind='manual')
            prefix = '✅ ' if ok else '⚠️ '
            v = with_delivery([v])[0]
            return await screen_visit(chat, mid, vid)
        if key == 'tickets':
            return await screen_tickets(chat, mid)
        if key == 'clients':
            return await screen_clients(chat, mid)
        if key == 'cl_refresh':
            CLIENT_CACHE['data'] = None
            return await screen_clients(chat, mid)
        if key.startswith('cllist:'):
            _, seg, page = key.split(':', 2)
            if seg in ('all', 'active', 'sleep', 'lost'):
                return await screen_client_list(chat, mid, seg, int(page))
        if key.startswith('client:'):
            return await screen_client(chat, mid, key[7:])
        if key.startswith('ticket_reply:'):
            tid = int(key.split(':', 1)[1])
            REPLY_WAITING[chat] = tid
            return await send(chat, f'💬 Ответ на #{tid}. Пришли текст или /cancel.')
        if key.startswith('ticket_close:'):
            tid = int(key.split(':', 1)[1])
            store.set_ticket_status(tid, 'closed')
            return await screen_ticket(chat, mid, tid)
        if key.startswith('ticket:'):
            return await screen_ticket(chat, mid, int(key.split(':', 1)[1]))
        if key.startswith('visit:'):
            return await screen_visit(chat, mid, key[6:])
        if key.startswith('mark_'):
            m = re.match(r'^mark_(sms|confirm|skip|pending):(.+)$', key)
            if m:
                typ, vid = m.groups()
                v = store.find_visit_any(vid)
                if not v:
                    return await screen_tomorrow(chat, mid)
                if typ == 'sms' and admin_notice(with_delivery([v])[0])['bucket'] != 'sms':
                    return await edit(chat, mid, 'Нет подтверждения недоставки ChatPush.',
                                      inline([[{'text': '📱 SMS', 'callback_data': 'manual_sms'}]]))
                new_state = {'sms': 'sms_sent', 'confirm': 'confirmed',
                             'skip': 'skipped', 'pending': 'pending'}[typ]
                store.update_manual_state(vid, v['day'], new_state)
                if typ in ('sms', 'confirm', 'skip'):
                    notify.close_manual_tasks(vid, v['day'],
                        {'sms': 'manual_sms_sent', 'confirm': 'manual_confirmed',
                         'skip': 'manual_skipped'}[typ])
                return await screen_visit(chat, mid, vid)
        if key.startswith('cp_mark_'):
            m = re.fullmatch(r'cp_mark_(delivered|failed):([0-9]+)', key)
            if not m:
                return
            state, vid = m.groups()
            day = crm.appointment_day()
            try:
                live = await asyncio.to_thread(crm.fetch_day, day)
            except Exception:
                return await edit(chat, mid, 'Нет свежего расписания.')
            visit = next((v for v in live if str(v.get('id')) == vid), None)
            if not visit:
                return await screen_chatpush_check(chat, mid)
            question = ('Telegram доставлен — исключить из SMS.' if state == 'delivered'
                        else 'Telegram НЕ доставлен — включить в SMS.')
            return await edit(chat, mid,
                f"Проверил(а) историю ChatPush для {visit['time']} · {visit['name']}?\n{question}",
                inline([[{'text': '✅ Сохранить', 'callback_data': f"cp_save_{state}:{vid}"}],
                        [{'text': '❌ Отмена', 'callback_data': f"visit:{vid}"}]]))
        if key.startswith('cp_save_'):
            m = re.fullmatch(r'cp_save_(delivered|failed):([0-9]+)', key)
            if not m:
                return
            state, vid = m.groups()
            day = crm.appointment_day()
            try:
                live = await asyncio.to_thread(crm.fetch_day, day)
            except Exception:
                return await edit(chat, mid, 'Нет свежего расписания.')
            visit = next((v for v in live if str(v.get('id')) == vid), None)
            if not visit:
                return await screen_chatpush_check(chat, mid)
            chatpush.reconcile_external(vid, day, visit['time'], state)
            return await screen_visit(chat, mid, vid)
    except Exception as e:
        log.exception('handle_callback failed for %s', key)
        try:
            return await send(chat, f'⚠️ Ошибка: {type(e).__name__}: {str(e)[:200]}')
        except Exception:
            pass


async def safe_notice(text):
    try:
        await send(int(cfg('admin_id')), text)
    except Exception as exc:
        log.error('Admin notice: %s', exc)


# ─── Основной цикл ─────────────────────────────────────────────────────────

async def main():
    if not cfg('bot_token'):
        raise RuntimeError('Нужен bot_token')
    store.initialize()
    notify.initialize()
    chatpush.initialize()
    me = await tg('getMe')
    log.info('%s · @%s', RELEASE, me.get('username', ''))
    try:
        url = telegram.app_url(telegram.live_url(), 'admin')
        if url:
            await asyncio.to_thread(telegram.set_menu, cfg('bot_token'), url)
            await tg('setChatMenuButton', {'chat_id': int(cfg('admin_id')),
                'menu_button': {'type': 'web_app', 'text': '🩷 NEFOR SPOT ADMIN',
                                'web_app': {'url': url}}})
    except Exception as ex:
        log.warning('Menu refresh: %s', ex)
    try:
        await tg('deleteWebhook', {'drop_pending_updates': False})
    except Exception as ex:
        log.warning('deleteWebhook: %s', ex)
    asyncio.create_task(auto_reminder_loop())
    offset = 0
    while True:
        try:
            updates = await tg('getUpdates',
                {'offset': offset, 'timeout': 10,
                 'allowed_updates': ['message', 'callback_query']},
                retries=2, timeout=17)
            for u in updates:
                offset = max(offset, u['update_id'] + 1)
                try:
                    if u.get('message'):
                        await handle_message(u['message'])
                    elif u.get('callback_query'):
                        await handle_callback(u['callback_query'])
                except Exception as ex:
                    log.exception('Update failed: %s', ex)
        except Exception as ex:
            log.warning('Polling reconnect: %s', ex)
            await asyncio.sleep(2)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
