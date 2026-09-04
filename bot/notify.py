"""Уведомления о состоянии бота: статус в беседу и в ЛС получателю уведомлений NOTIFY_VK_ID (с @упоминанием),
при падении ещё и письмо. Вызывается из main.py (старт/рестарт/крэш) или
systemd-юнитом (OnFailure): python -m bot.notify

VK шлём голым HTTPS-вызовом без vkbottle: библиотека сама может быть причиной падения.
"""
import json
import os
import random
import smtplib
import socket
import sqlite3
import tempfile
import time
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import make_msgid

from dotenv import load_dotenv

from bot.db import DB_PATH

load_dotenv()

BODY = 'Браза, я упал, подними меня пожалуйста!'
COOLDOWN = 15 * 60  # секунд между уведомлениями — crash-loop не спамит
_STAMP = os.path.join(tempfile.gettempdir(), 'vkstats_crash_mail.stamp')
API = 'https://api.vk.com/method/'
_mention: str | None = None


def notify_crash(reason: str = ''):
    """Письмо + статус VK, каждое в своём try — одно не мешает другому."""
    try:
        if time.time() - os.path.getmtime(_STAMP) < COOLDOWN:
            print('уведомление уже отправлялось недавно — пропуск')
            return
    except OSError:
        pass
    try:
        send_crash_mail(reason)
    except Exception as e:
        print(f'письмо не отправлено: {e!r}')
    try:
        send_status_vk('Я упал', detail=reason)
    except Exception as e:
        print(f'статус VK не отправлен: {e!r}')
    with open(_STAMP, 'w'):
        pass


def send_crash_mail(reason: str = '') -> bool:
    user = os.getenv('SMTP_USER')
    password = os.getenv('SMTP_PASS')
    if not (user and password):
        print('SMTP_USER/SMTP_PASS не заданы — письмо не отправлено')
        return False
    msg = EmailMessage()
    # без Message-ID Gmail не склеивает копии «Отправленные»/«Входящие» при письме самому себе
    msg['Message-ID'] = make_msgid()
    msg['Subject'] = f'VK-бот упал на {socket.gethostname()}'
    msg['From'] = user
    msg['To'] = os.getenv('NOTIFY_EMAIL') or user
    msg.set_content(BODY + '\n\n' + (reason or 'Логи: journalctl -u vkstats'))

    with smtplib.SMTP(os.getenv('SMTP_HOST', 'smtp.gmail.com'),
                      int(os.getenv('SMTP_PORT', '587')), timeout=30) as s:
        s.starttls()
        s.login(user, password)
        s.send_message(msg)
    print(f'письмо отправлено на {msg["To"]}')
    return True


def _vk(method: str, token: str, **params) -> dict:
    """Один вызов VK API; при ошибке API — RuntimeError с текстом."""
    params.update(access_token=token, v='5.199')
    req = urllib.request.Request(API + method, data=urllib.parse.urlencode(params).encode())
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.load(r)
    if 'error' in resp:
        # 901 = юзер не разрешил сообщения от сообщества — надо написать боту в ЛС один раз
        raise RuntimeError(resp['error'].get('error_msg'))
    return resp['response']


def mention(token: str, user_id: int) -> str:
    """'[id123|@screen_name]' — кликабельное упоминание получателя (NOTIFY_VK_ID); screen_name
    тянем из API один раз на процесс, при сбое — '@id123'."""
    global _mention
    if _mention is None:
        try:
            u = _vk('users.get', token, user_ids=user_id, fields='screen_name')[0]
            name = u.get('screen_name') or f'id{user_id}'
        except Exception:
            name = f'id{user_id}'
        _mention = f'[id{user_id}|@{name}]'
    return _mention


def chat_peer() -> int | None:
    """peer_id основной беседы из settings (см. main.should_log); None — БД ещё пуста."""
    try:
        with sqlite3.connect(DB_PATH) as c:
            row = c.execute("SELECT value FROM settings WHERE key = 'peer_id'").fetchone()
        return int(row[0]) if row else None
    except sqlite3.Error:
        return None


def send_status_vk(text: str, detail: str = '') -> bool:
    """'{text} @NOTIFY_VK_ID' в беседу и в ЛС этому же id; detail (трейсбек) — только в ЛС.
    True — хоть куда-то ушло."""
    token = os.getenv('VK_TOKEN')
    user_id = os.getenv('NOTIFY_VK_ID')
    if not (token and user_id):
        print('VK_TOKEN/NOTIFY_VK_ID не заданы — статус не отправлен')
        return False
    user_id = int(user_id)
    text = f'{text} {mention(token, user_id)}'
    targets = [(chat_peer(), text), (user_id, text + ('\n\n' + detail[:3500] if detail else ''))]
    ok = False
    for peer, msg in targets:
        if peer is None:
            continue
        try:
            _vk('messages.send', token, peer_id=peer, random_id=random.randint(1, 2**31), message=msg)
            ok = True
        except Exception as e:
            print(f'статус в peer {peer} не отправлен: {e!r}')
    return ok


if __name__ == '__main__':
    notify_crash()
