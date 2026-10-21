"""Клиентские операции с визитами."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import json

import crm
import storage as store
from crm import BookingError

MSK = timezone(timedelta(hours=3))

def my_visits(uid: int) -> dict:
    p = store.profile(uid) or {}
    verified = bool(p.get('phone_verified') and p.get('phone'))
    
    if not verified:
        return {'visits': [], 'requires_phone': True, 'fresh': True, 'pending_actions': []}
        
    phone = store.norm_phone(p['phone'])
    try:
        state = crm.get_state(force=True)
    except Exception:
        return {'visits': [], 'requires_phone': False, 'fresh': False, 'message': 'CRM недоступна', 'pending_actions': []}
        
    masters_map = {str(m['id']): m['name'] for m in state.get('masters', [])}
    services_map = {str(s['id']): s['name'] for s in state.get('services', [])}
    
    visits = []
    for a in state.get('appts', []):
        if a.get('block') or a.get('status') == 'cancelled':
            continue
            
        if store.norm_phone(a.get('phone', '')) == phone:
            visits.append({
                'ref': str(a['id']),
                'visitID': str(a['id']),
                'date': a['date'],
                'time': a['time'],
                'status': a.get('status', 'new'),
                'master': masters_map.get(str(a.get('masterId')), '—'),
                'service': services_map.get(str(a.get('serviceId')), 'Услуга'),
                'fresh': True,
                'source': 'crm',
                'can_cancel': True,
                'can_reschedule': False
            })
            
    visits.sort(key=lambda v: (v['date'], v['time']))
    return {'visits': visits, 'requires_phone': False, 'fresh': True, 'pending_actions': []}


def calendar(uid: int, ref: str, day: str = '') -> dict:
    # Перенос в твоей CRM пока не реализован через API, возвращаем пустоту
    return {'dates': [], 'slots': []}


def action_status(uid: int, key: str) -> dict:
    return {'confirmed': True, 'state': 'done', 'message': 'Готово'}


def change(uid: int, data: dict) -> dict:
    if data.get('confirmed') is not True or data.get('action') != 'cancel':
        raise BookingError('Неподдерживаемое действие.', 400)
    
    appt_id = data.get('ref')
    if not appt_id:
        raise BookingError('Ошибка ID записи.', 400)
        
    try:
        crm._req(f'/api/appts/{appt_id}/cancel', method='POST')
        crm._CACHE['at'] = 0 # сброс кеша
    except BookingError as e:
        raise BookingError('Не удалось отменить запись в CRM.', e.status_code)
        
    return {'ok': True, 'confirmed': True, 'state': 'done', 'message': 'Запись успешно отменена.'}
