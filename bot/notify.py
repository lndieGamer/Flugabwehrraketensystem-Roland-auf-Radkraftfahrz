"""Уведомления о падении бота: письмо + ЛС ВКонтакте из сообщества.
Вызывается из main.py при крэше или systemd-юнитом (OnFailure): python -m bot.notify

VK шлём голым HTTPS-вызовом без vkbottle: библиотека сама может быть причиной падения.
"""
import json
import os
import random
import smtplib
import socket
import tempfile
import time
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import make_msgid

from dotenv import load_dotenv

load_dotenv()

BODY = 'Браза, я упал, подними меня пожалуйста!'
COOLDOWN = 15 * 60  # секунд между уведомлениями — crash-loop не спамит
_STAMP = os.path.join(tempfile.gettempdir(), 'vkstats_crash_mail.stamp')


def notify_crash(reason: str = ''):
    """Письмо + ЛС VK, каждое в своём try — одно не мешает другому."""
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
        send_crash_vk(reason)
    except Exception as e:
        print(f'ЛС VK не отправлено: {e!r}')
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


def send_crash_vk(reason: str = '') -> bool:
    token = os.getenv('VK_TOKEN')
    user_id = os.getenv('NOTIFY_VK_ID')
    if not (token and user_id):
        print('VK_TOKEN/NOTIFY_VK_ID не заданы — ЛС не отправлено')
        return False
    text = BODY + ('\n\n' + reason[:3500] if reason else '')
    params = urllib.parse.urlencode({
        'access_token': token, 'v': '5.199',
        'user_id': int(user_id),
        'random_id': random.randint(1, 2**31),
        'message': text,
    }).encode()
    req = urllib.request.Request('https://api.vk.com/method/messages.send', data=params)
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.load(r)
    if 'error' in resp:
        # 901 = юзер не разрешил сообщения от сообщества — надо написать боту в ЛС один раз
        print(f'VK не принял ЛС: {resp["error"].get("error_msg")}')
        return False
    print(f'ЛС отправлено пользователю id{user_id}')
    return True


if __name__ == '__main__':
    notify_crash()
