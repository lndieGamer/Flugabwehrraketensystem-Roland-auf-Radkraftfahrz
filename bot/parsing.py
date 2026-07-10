"""Общая классификация сообщений: используется и импортёром истории, и живым ботом."""
import re

HEADER_RE = re.compile(
    r'^(\d{2})\.(\d{2})\.(\d{4}), (\d{2}):(\d{2}):(\d{2}) \| (.+?) \| cmid: (\d+)\s*$'
)
SENDER_RE = re.compile(r'^(.*?) \(vk\.ru/(id|club)(\d+)\)$')

REPLY_RE = re.compile(r'^\[Ответ на сообщение: cmid (\d+)\]$')
PHOTO_RE = re.compile(r'^\[Фото \(https?://[^)]*\)\]$')
VOICE_RE = re.compile(r'^\[Голосовое сообщение \(https?://[^)]*\)\]$')
VIDEO_MSG_RE = re.compile(r'^\[Видеосообщение \(https?://[^)]*\)\]$')
NO_TEXT = '[Нет текста]'
STICKER = '[Стикер]'
OTHER_ATTACHMENT = '[Другое вложение]'
FORWARDED = '[Пересланные сообщения]'

WORD_RE = re.compile(r'\w+')


def count_words(text: str) -> int:
    return len(WORD_RE.findall(text))


def classify_body(lines) -> dict:
    """Разбирает строки тела сообщения: маркеры вложений отделяются от текста.

    has_attachment = вложение, отличное от фото/стикера (войс, видеосообщение,
    форвард, прочее). Фото и стикер имеют собственные флаги.
    """
    info = dict(text='', word_count=0, is_reply=0, reply_to_cmid=None,
                has_sticker=0, has_photo=0, has_attachment=0)
    text_lines = []
    for line in lines:
        s = line.strip()
        m = REPLY_RE.match(s)
        if m:
            info['is_reply'] = 1
            info['reply_to_cmid'] = int(m.group(1))
            continue
        if s == NO_TEXT:
            continue
        if PHOTO_RE.match(s):
            info['has_photo'] = 1
            continue
        if s == STICKER:
            info['has_sticker'] = 1
            continue
        if s in (OTHER_ATTACHMENT, FORWARDED) or VOICE_RE.match(s) or VIDEO_MSG_RE.match(s):
            info['has_attachment'] = 1
            continue
        text_lines.append(line)
    info['text'] = '\n'.join(text_lines).strip()
    info['word_count'] = count_words(info['text'])
    return info


def parse_export(lines):
    """Генератор блоков из выгрузки. Блок = заголовок + тело до следующего заголовка
    (граница — заголовок, а не пустая строка: пустые строки внутри текста не ломают разбор).

    Служебные события (`Действие "..."`) отдаются с service=True.
    """
    header, body = None, []
    for line in lines:
        line = line.rstrip('\n')
        m = HEADER_RE.match(line)
        if m:
            if header:
                yield _make_block(header, body)
            header, body = m, []
        elif header is not None:
            body.append(line)
    if header:
        yield _make_block(header, body)


def _make_block(m: re.Match, body: list[str]) -> dict:
    d, mo, y, hh, mi, ss, sender, cmid = m.groups()
    ts = f'{y}-{mo}-{d}T{hh}:{mi}:{ss}'
    sm = SENDER_RE.match(sender)
    if sender.startswith('Действие') or not sm:
        return {'service': True, 'cmid': int(cmid), 'ts': ts, 'sender_raw': sender}
    name, kind, num = sm.group(1), sm.group(2), int(sm.group(3))
    # сообщества (club...) храним с отрицательным vk_id — как отдаёт живой Long Poll
    block = {
        'service': False, 'cmid': int(cmid), 'ts': ts,
        'vk_id': -num if kind == 'club' else num,
        'display_name': name,
        'is_community': 1 if kind == 'club' else 0,
    }
    while body and not body[-1].strip():
        body.pop()
    block.update(classify_body(body))
    return block
