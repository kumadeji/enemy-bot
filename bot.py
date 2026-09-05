import discord
import os
import sys
import signal
import atexit

# На Windows, если stdout/stderr перенаправлены в файл (а не в реальную консоль),
# Python выбирает кодировку по системной ANSI-кодовой странице (часто cp1251),
# которая физически не может отобразить эмодзи (✅, ❌, 🔴 и т.д.) — это вызывает
# необработанный UnicodeEncodeError и падение процесса ещё ДО подключения к Discord.
# Принудительно переключаем оба потока на UTF-8 в самом начале, до первого print().
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

import logging
import threading as _threading_early
from collections import deque as _deque_early

# ============== ПЕРЕСЫЛКА ЛОГОВ БОТА В СПЕЦИАЛЬНУЮ ВЕТКУ DISCORD ==============
# Все print()-сообщения бота (✅/❌/⚠️ и т.д.) и системные логи discord.py
# (подключения/переподключения к gateway, rate limit, необработанные ошибки
# во view/modal) дублируются в специальную ветку — LOG_THREAD_ID. Публикация
# идёт пачками раз в 15 секунд, чтобы не спамить и не упираться в rate limit.

LOG_FORWARD_LEVEL = logging.INFO  # при желании поднять до logging.WARNING, если шумно
# LOG_THREAD_ID больше НЕ используется как фиксированная константа — теперь
# ветка логов находится динамически через якорное сообщение "🔧 Логирование"
# в ADMIN_CHANNEL_ID (см. ensure_admin_channel_anchors). Значение ниже — просто
# запасной ID на случай самого первого запуска ДО того, как якоря созданы
# (форвардер логов стартует раньше, чем on_ready успевает создать якоря).
_FALLBACK_LOG_THREAD_ID = None

_original_print = print  # сохраняем оригинальный print ДО подмены
_log_forward_buffer = _deque_early(maxlen=2000)
_log_forward_lock = _threading_early.Lock()


def _enqueue_log_line(line: str):
    with _log_forward_lock:
        _log_forward_buffer.append(line)


def print(*args, **kwargs):
    """Подменённый print(): работает как обычно + дублирует строку в очередь
    на пересылку в Discord-ветку (см. flush_log_buffer_to_discord)."""
    sep = kwargs.get('sep', ' ')
    try:
        _enqueue_log_line(sep.join(str(a) for a in args))
    except Exception:
        pass
    _original_print(*args, **kwargs)


class _DiscordLogHandler(logging.Handler):
    """Перехватывает системные логи discord.py (discord.client, discord.gateway,
    discord.http, discord.ui.view и т.д.) — то есть подключения, rate limit
    и необработанные ошибки во view/modal, которые НЕ идут через наш print()."""
    def emit(self, record):
        try:
            _enqueue_log_line(self.format(record))
        except Exception:
            pass


def _setup_discord_log_forwarding():
    handler = _DiscordLogHandler()
    handler.setLevel(LOG_FORWARD_LEVEL)
    handler.setFormatter(logging.Formatter(
        '[%(asctime)s] [%(levelname)-8s] %(name)s: %(message)s', datefmt='%Y-%m-%d %H:%M:%S'
    ))
    logging.getLogger('discord').addHandler(handler)


_setup_discord_log_forwarding()

_log_forward_thread_cache = None


async def flush_log_buffer_to_discord():
    """Периодически (см. job в on_ready) отправляет накопленные строки логов
    пачками в ветку якорного сообщения '🔧 Логирование' (ADMIN_CHANNEL_ID).
    До того, как якорь создан (самое начало запуска бота), строки просто
    копятся в буфере — ничего не теряется, они улетят при первом же
    успешном определении ветки."""
    global _log_forward_thread_cache
    with _log_forward_lock:
        if not _log_forward_buffer:
            return
        lines = list(_log_forward_buffer)
        _log_forward_buffer.clear()
    if not lines:
        return
    try:
        if _log_forward_thread_cache is None:
            thread_id = await get_logging_thread_id()
            if not thread_id:
                # Якорь ещё не создан (например, это самое начало запуска) —
                # возвращаем строки обратно в буфер, попробуем в следующий раз.
                with _log_forward_lock:
                    for line in reversed(lines):
                        _log_forward_buffer.appendleft(line)
                return
            _log_forward_thread_cache = await client.fetch_channel(thread_id)
        thread = _log_forward_thread_cache
        text = "\n".join(lines)
        chunk_size = 1900
        for i in range(0, len(text), chunk_size):
            chunk = text[i:i + chunk_size]
            await thread.send(f"```\n{chunk}\n```")
            await asyncio.sleep(0.5)
    except Exception as e:
        _original_print(f"⚠️ Не удалось отправить логи в Discord-ветку: {e}")

import gspread

import re
import json
import uuid
import copy
import threading
from datetime import datetime, timedelta
from collections import deque
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import asyncio
from concurrent.futures import ThreadPoolExecutor
import firebase_admin
from firebase_admin import credentials as firebase_credentials, firestore
from google.cloud.firestore_v1.base_query import FieldFilter


def _disable_windows_quick_edit_mode():
    """На Windows клик мышью в окне консоли включает режим 'выделения текста'
    (QuickEdit Mode), который на уровне ОС ставит на паузу ЛЮБОЙ вызов
    чтения/записи консоли (ReadConsole/WriteConsole) — а это блокирует
    ВЕСЬ процесс целиком, включая event loop бота и все потоки executor'а,
    пока пользователь не нажмёт Esc/Enter. Именно это вызывает многочасовые
    'зависания' бота (heartbeat blocked -> разрыв gateway-сессии Discord).
    Отключаем QuickEdit Mode программно, чтобы клики по консоли были безопасны."""
    if os.name != 'nt':
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        STD_INPUT_HANDLE = -10
        ENABLE_QUICK_EDIT_MODE = 0x0040
        ENABLE_EXTENDED_FLAGS = 0x0080

        h_stdin = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(h_stdin, ctypes.byref(mode)):
            return
        new_mode = (mode.value & ~ENABLE_QUICK_EDIT_MODE) | ENABLE_EXTENDED_FLAGS
        kernel32.SetConsoleMode(h_stdin, new_mode)
        print("🖱️ Windows QuickEdit Mode отключён (защита от зависания при клике в консоль)")
    except Exception as e:
        print(f"⚠️ Не удалось отключить QuickEdit Mode: {e}")


_disable_windows_quick_edit_mode()

# Глобальный executor для синхронных операций (gspread использует requests)
EXECUTOR = ThreadPoolExecutor(max_workers=5)

# ============== МОДУЛЬНОСТЬ ХРАНИЛИЩА ДАННЫХ (Firebase / локальный JSON) ==============
# Переключатель режима хранения СОБСТВЕННЫХ данных бота (мероприятия, отпуска,
# явка, еженедельные мероприятия, временные голосовые комнаты, метки планировщика).
#
#   'firebase' (по умолчанию) — читаем и пишем в Firebase. При КАЖДОЙ записи
#                                параллельно обновляется зеркальная резервная
#                                копия в локальных JSON-файлах — на случай,
#                                если Firebase станет недоступен и придётся
#                                аварийно переключиться на них.
#   'json'                    — работаем ИСКЛЮЧИТЕЛЬНО с локальными JSON-файлами,
#                                подключение к Firebase для этих данных вообще
#                                не устанавливается.
#
# ВАЖНО: переключатель касается только данных, которые генерирует сам бот.
# Список бойцов клана (rosterPublic), очередь на командование (queue/state),
# учёт отыгрышей (profiles/gameStats) и публикация новых анкет/уведомлений/
# changeLog — это данные САЙТА клана, которые физически существуют только
# в Firebase; при DATA_BACKEND='json' эти функции автоматически и безопасно
# отключаются (без падений бота), так как эквивалента в JSON для них нет.
#
# Переключить можно прямо здесь (задать 'json') либо переменной окружения:
#   set DATA_BACKEND=json      (Windows)
#   export DATA_BACKEND=json  (Linux)
DATA_BACKEND = os.environ.get('DATA_BACKEND', 'firebase').strip().lower()
if DATA_BACKEND not in ('firebase', 'json'):
    print(f"⚠️ Некорректное значение DATA_BACKEND='{DATA_BACKEND}', использую 'firebase' по умолчанию.")
    DATA_BACKEND = 'firebase'
USE_FIREBASE_BACKEND = (DATA_BACKEND == 'firebase')
print(f"🗄️ Режим хранения данных бота: {DATA_BACKEND.upper()}")

# ============== ЗАЩИТА ОТ ДВОЙНОГО ЗАПУСКА ==============

LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.bot.lock')

_lock_file_handle = None  # держим открытым на весь срок жизни процесса


def _release_instance_lock():
    """Освобождает lock-файл. Вынесена на уровень модуля (не вложена в
    acquire_single_instance_lock), чтобы её можно было вызвать вручную —
    например, при принудительном перезапуске через админ-панель."""
    global _lock_file_handle
    try:
        if _lock_file_handle and not _lock_file_handle.closed:
            if os.name == 'nt':
                import msvcrt
                try:
                    _lock_file_handle.seek(0)
                    msvcrt.locking(_lock_file_handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl
                fcntl.flock(_lock_file_handle.fileno(), fcntl.LOCK_UN)
            _lock_file_handle.close()
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except Exception as e:
        print(f"Ошибка при удалении lock-файла: {e}")


def acquire_single_instance_lock():
    """OS-level эксклюзивная блокировка файла."""
    global _lock_file_handle
    _lock_file_handle = open(LOCK_FILE, 'a+')
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(_lock_file_handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(_lock_file_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"❌ Бот уже запущен (файл блокировки '{LOCK_FILE}' занят другим процессом). Останавливаю этот процесс.")
        sys.exit(1)

    _lock_file_handle.seek(0)
    _lock_file_handle.truncate()
    _lock_file_handle.write(str(os.getpid()))
    _lock_file_handle.flush()

    atexit.register(_release_instance_lock)

    def _signal_handler(signum, frame):
        print(f"Получен сигнал {signum}, завершаю работу...")
        _release_instance_lock()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)


acquire_single_instance_lock()


# ============== УТИЛИТА ДЛЯ ЗАМЕНЫ ПРОБЕЛОВ ПОСЛЕ ЭМОДЗИ ==============

def es(text):
    """Заменяет обычный пробел после эмодзи на символ ㅤ (U+3164)"""
    replacements = {
        '🔔 ': '🔔ㅤ', '📌 ': '📌ㅤ', '✅ ': '✅ㅤ', '❌ ': '❌ㅤ',
        '❓ ': '❓ㅤ', '💡 ': '💡ㅤ', '🏖️ ': '🏖️ㅤ', '🏖 ': '🏖ㅤ',
        '📅 ': '📅ㅤ', '📝 ': '📝ㅤ', '🗑️ ': '🗑️ㅤ', '🔄 ': '🔄ㅤ',
        '🔍 ': '🔍ㅤ', '⏰ ': '⏰ㅤ', '👤 ': '👤ㅤ', '🛠️ ': '🛠️ㅤ',
        '⚡ ': '⚡ㅤ', '📍 ': '📍ㅤ', '👉 ': '👉ㅤ', '📤 ': '📤ㅤ',
        '⚠️ ': '⚠️ㅤ', '🔴 ': '🔴ㅤ', '🟡 ': '🟡ㅤ', 'ℹ️ ': 'ℹ️ㅤ',
        '⛔ ': '⛔ㅤ', '🎯 ': '🎯ㅤ', '🔧 ': '🔧ㅤ', '💬 ': '💬ㅤ',
        '📋 ': '📋ㅤ', '📖 ': '📖ㅤ', '📞 ': '📞ㅤ', '✏️ ': '✏️ㅤ',
        '💥 ': '💥ㅤ', '⭐ ': '⭐ㅤ', '🧪 ': '🧪ㅤ', '📭 ': '📭ㅤ',
        '📢 ': '📢ㅤ', '⏳ ': '⏳ㅤ', '🎮 ': '🎮ㅤ', '👥 ': '👥ㅤ',
        '🕹️ ': '🕹️ㅤ', '🏆 ': '🏆ㅤ', '🪖 ': '🪖ㅤ', '🔵 ': '🔵ㅤ',
        '🍻 ': '🍻ㅤ', '🚪 ': '🚪ㅤ', '➡️ ': '➡️ㅤ', '⏭️ ': '⏭️ㅤ',
        '🖼️ ': '🖼️ㅤ', '🚫 ': '🚫ㅤ', '🎖️ ': '🎖️ㅤ', '🧩 ': '🧩ㅤ',
        '🏁 ': '🏁ㅤ', '🚀 ': '🚀ㅤ', '🔁 ': '🔁ㅤ',
    }
    if not isinstance(text, str):
        return text
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


# ============== НАСТРОЙКИ ==============

THREAD_ID = 1530860224724996237
PREFIX = '!s '

ADMIN_USER_IDS = [
    316641571284058113,
    766919669838905364,
    115475534544109573
]

GOOGLE_CREDENTIALS_FILE = 'credentials.json'
SPREADSHEET_URL = 'https://docs.google.com/spreadsheets/d/1QGc-SRkWnFCaSx56_46UJPRK0XOe33KPou7yJznbQBM'

MSK = pytz.timezone('Europe/Moscow')

NICKNAME_COLUMN = 'Discord клана (с клантегом)'
SHEET_NAME = 'Основная таблица'

COLUMNS_TO_CHECK = [
    'Discord клана (с клантегом)', 'Discord ECHO (с клантегом)',
    'Discord AS VDV (с клантегом)', 'Discord TT (с клантегом)',
    'Steam (с клантегом)', 'Steam (в друзьях у BURBON?)',
    'Сайт клана (без клантега)', 'Сайт ECHO (без клантега)',
    'Сайт AS VDV (без клантега)', 'Сайт TT (без клантега - исправить только через администрацию)'
]

EXPECTED_INTRO_MAX_LEN = 700

# ============== НОВЫЕ НАСТРОЙКИ ==============

EVENTS_CHANNEL_ID = 1311705378140196926
VACATION_CHANNEL_ID = 1284905224099598407
ADMIN_CHANNEL_ID = 1536632416511332362
ANKETA_CHANNEL_ID = 1366767440939454504

# ============== ЕДИНЫЙ РЕЕСТР РОЛЕЙ (стандартизация) ==============
# Роли с фиксированным ID собраны здесь в одном месте — это единственный
# источник правды. Роли без фиксированного ID (создаются ботом динамически,
# например "Отпуск", либо их ID нам неизвестен, например "Боец ArmA")
# по-прежнему ищутся по имени, но теперь через ОДНУ общую функцию
# get_role_by_name() вместо трёх разных копий этого кода по всему файлу.

ROLE_IDS = {
    'kombat_arma': 1252277370711441429,
    'zam_kombat_arma': 1470351490005729383,
    'kombat_squad': 1230600119053848787,
    'zam_kombat_squad': 1503144759084974150,
    'boets_arma': 1284456321005129778,
    'otpusk': 1536500577553481768,
    'zapas_arma': 1496435854531366913,
    'clan_enemy': 1048133924645240903,
    'lichnyj_sostav_arma': 1449782732480577696,
    'minomyotchik_arma': 1444670257565405337,
    'btv_arma': 1444670373768593509,
    'pilot_arma': 1444670451409490090,
    'vzvozdnyj_ks_arma': 1448035996653588614,
    'bpla_pilot_arma': 1474032938847834306,
    'ko_arma': 1528120615821905960,
}

EXPULSION_ROLE_KEYS = [
    'zapas_arma', 'boets_arma', 'clan_enemy', 'lichnyj_sostav_arma',
    'minomyotchik_arma', 'btv_arma', 'pilot_arma', 'vzvozdnyj_ks_arma',
    'bpla_pilot_arma', 'ko_arma',
]

# ============== АВТОМАТИЧЕСКИЕ ЗАМЕЧАНИЯ/ВЫГОВОРЫ ЗА НЕАКТИВНОСТЬ ==============

DISCIPLINE_CUTOFF = MSK.localize(datetime(2026, 9, 7, 0, 0, 0))
MAX_ACTIVE_WARNINGS = 3
MAX_ACTIVE_REPRIMANDS = 3
WARNING_DURATION = timedelta(days=30)
REPRIMAND_DURATION = timedelta(days=90)


def get_role_mention_by_id(guild, role_key: str):
    """Mention роли по ключу из ROLE_IDS, либо None, если роли нет на сервере
    (защита от 'битых' упоминаний, если ID устарел или роль удалили)."""
    role_id = ROLE_IDS.get(role_key)
    if not role_id or not guild:
        return None
    role = guild.get_role(role_id)
    return role.mention if role else None


def get_role_by_name(guild, name: str):
    """Единая точка поиска ролей БЕЗ фиксированного ID (по имени)."""
    return discord.utils.get(guild.roles, name=name)


def get_leadership_mentions(guild, *role_keys: str) -> str:
    """Собирает через пробел mentions нескольких ролей из ROLE_IDS,
    пропуская те, которых не оказалось на сервере."""
    mentions = [m for m in (get_role_mention_by_id(guild, key) for key in role_keys) if m]
    return " ".join(mentions)


def get_anketa_leadership_mentions(guild, games_interested: list) -> str:
    """Кого тегать под новой анкетой в зависимости от игр, которыми
    интересуется кандидат:
    Arma Reforger -> Комбат ArmA + Зам. комбата ArmA
    Squad         -> Комбат SQUAD + Зам. комбата SQUAD
    Обе игры      -> все четыре роли
    Ни одна из известных -> по умолчанию руководство ArmA."""
    games_lower = {(g or '').strip().lower() for g in (games_interested or [])}
    keys = []
    if 'arma reforger' in games_lower:
        keys.extend(['kombat_arma', 'zam_kombat_arma'])
    if 'squad' in games_lower:
        keys.extend(['kombat_squad', 'zam_kombat_squad'])
    if not keys:
        keys = ['kombat_arma', 'zam_kombat_arma']
    return get_leadership_mentions(guild, *keys)


VOICE_CHANNEL_ID = 1284893513921728582

VOICE_ROOM_CATEGORY_ARMY = 1284893244878098464
VOICE_ROOM_CATEGORY_PUBLIC = 1116657923360301157

EVENTS_FILE = 'events_data.json'
VACATIONS_FILE = 'vacations.json'
ATTENDANCE_FILE = 'attendance_data.json'
WEEKLY_EVENTS_FILE = 'weekly_events.json'
LAST_SCHEDULED_CHECK_FILE = 'last_scheduled_check.json'
VOICE_ROOMS_FILE = 'voice_rooms.json'
CHECK_MESSAGES_FILE = 'check_messages.json'
ADMIN_ANCHORS_FILE = 'admin_anchors.json'

# ============== FIREBASE ==============

FIREBASE_CREDENTIALS_FILE = 'credentials_firebase.json'
FIREBASE_PROJECT_ID = 'enemy-firebase'
FIREBASE_ROSTER_COLLECTION = 'rosterPublic'

# Клантег, которым дополняется "голый" позывной (callsign) из Firebase,
# чтобы получить строку, сравнимую с discord display_name (напр. "[En-Y]Killa").
# Если у вас в клане используется другой тег — поменяйте здесь.
CLAN_TAG = "[En-Y]"

# Игра, по которой бот определяет принадлежность к клану (composition).
# Сейчас бот поддерживает только направление Arma Reforger.
CLAN_ROSTER_GAME = "Arma Reforger"

# Только эти composition считаются ДЕЙСТВУЮЩИМИ игроками клана.
# Остальные (например, "Отбор", "Кандидат" и т.п.) физически не состоят
# в клане и не должны иметь никаких прав бойца клана в боте.
ACTIVE_CLAN_COMPOSITIONS = {"Личный состав", "Запас"}

# Все "JSON-файлы" бота на самом деле хранятся как документы в Firestore,
# в коллекции botData. Ключ словаря — старое имя файла (для обратной совместимости
# со всем остальным кодом бота, который вызывает load_json('events_data.json', ...)
# и не подозревает о Firebase), значение — имя документа в Firestore.
FIREBASE_DATA_MAP = {
    EVENTS_FILE: 'events',
    VACATIONS_FILE: 'vacations',
    ATTENDANCE_FILE: 'attendance',
    WEEKLY_EVENTS_FILE: 'weeklyEvents',
    VOICE_ROOMS_FILE: 'voiceRooms',
    LAST_SCHEDULED_CHECK_FILE: 'lastScheduledCheck',
    CHECK_MESSAGES_FILE: 'checkMessages',
    ADMIN_ANCHORS_FILE: 'adminAnchors',
}


# ============== ЕЖЕНЕДЕЛЬНЫЕ МЕРОПРИЯТИЯ: СПРАВОЧНИКИ ==============

WEEKDAY_NAMES = {
    'mon': 'Понедельник', 'tue': 'Вторник', 'wed': 'Среда',
    'thu': 'Четверг', 'fri': 'Пятница', 'sat': 'Суббота', 'sun': 'Воскресенье'
}
WEEKDAY_INDEX = {'mon': 0, 'tue': 1, 'wed': 2, 'thu': 3, 'fri': 4, 'sat': 5, 'sun': 6}

DEFAULT_WEEKLY_EVENTS = {
    "weekly_asvdv_rtvt": {
        "name": "Плановые RTvT на AS VDV",
        "description": "Бойцы, в субботу пройдут плановые ротационные матчи Realistic TvT на сервере AS VDV. Матчи длинные - каждый по 60-90 минут. Ждём вас!",
        "day_of_week": "sat",
        "start_time": "16:30",
        "end_time": "19:30",
        "image_key": "asvdv",
        "num_games": 2,
        "mandatory": True
    },
    "weekly_tt_tvt": {
        "name": "Плановые TvT на Triad Tactics",
        "description": "Бойцы, в воскресенье пройдут плановые ротационные матчи TvT на сервере Triad Tactics. Матчи длинные - каждый по 60-90 минут. Ждём вас!",
        "day_of_week": "sun",
        "start_time": "17:45",
        "end_time": "22:15",
        "image_key": "tt",
        "num_games": 3,
        "mandatory": True
    }
}


def ensure_weekly_events_file():
    existing = load_json(WEEKLY_EVENTS_FILE, {})
    if not existing:
        save_json(WEEKLY_EVENTS_FILE, DEFAULT_WEEKLY_EVENTS)


def get_next_weekday_datetime(day_key: str, hour: int, minute: int, from_time: datetime = None) -> datetime:
    """Находит ближайшую дату/время для дня недели (day_key) начиная с from_time (или текущего момента)."""
    if from_time is None:
        from_time = datetime.now(MSK)
    target_weekday = WEEKDAY_INDEX[day_key]
    days_ahead = (target_weekday - from_time.weekday()) % 7
    candidate = (from_time + timedelta(days=days_ahead)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= from_time:
        candidate += timedelta(days=7)
    return candidate

MAX_GAMES = 10
MAX_SELECT_OPTIONS = 25

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMAGES_DIR = os.path.join(BASE_DIR, 'images')

EVENT_IMAGES = {
    'echo': {'file': 'echo-rounded.png', 'title': 'Матчи на ECHO'},
    'asvdv': {'file': 'asvdv-rounded.png', 'title': 'Матчи на AS VDV'},
    'tt': {'file': 'tt-rounded.png', 'title': 'Матчи на Triad Tactics'},
    'mezhklan': {'file': 'mezhklan-rounded.png', 'title': 'Межклановое мероприятие'},
    'vnutriklan': {'file': 'vnutriklan-rounded.png', 'title': 'Внутриклановое мероприятие'},
    'vylazka': {'file': 'vylazka-rounded.png', 'title': 'Клановая вылазка'},
    'mangust': {'file': 'mangust-rounded.png', 'title': 'Операция «Мангуст»'},
}

def get_image_info(image_key: str):
    if image_key == 'none' or image_key not in EVENT_IMAGES:
        return None, None
    filename = EVENT_IMAGES[image_key]['file']
    path = os.path.join(IMAGES_DIR, filename)
    if os.path.exists(path):
        return filename, path
    return None, None


def pluralize_games(num: int) -> str:
    if num <= 0:
        return "матчей"
    last_digit = num % 10
    last_two_digits = num % 100
    if 11 <= last_two_digits <= 14:
        return "матчей"
    if last_digit == 1:
        return "матч"
    elif last_digit in [2, 3, 4]:
        return "матча"
    else:
        return "матчей"


def pluralize_days(num: int) -> str:
    if num <= 0:
        return "дней"
    last_digit = num % 10
    last_two_digits = num % 100
    if 11 <= last_two_digits <= 14:
        return "дней"
    if last_digit == 1:
        return "день"
    elif last_digit in [2, 3, 4]:
        return "дня"
    else:
        return "дней"


def format_vacation_period(start_iso: str, end_iso: str) -> str:
    """Форматирует период отпуска в виде меток времени Discord.
    
    Первая строка: Даты: <t:START:d> - <t:END:d> (N день/дня/дней)
    Вторая строка (только когда актуален):
    - До начала: "Начнется:" + <t:START:R>
    - Во время: "Закончится:" + <t:END:R>
    - После окончания: исчезает
    """
    start = datetime.fromisoformat(start_iso)
    end = datetime.fromisoformat(end_iso)
    current_time = datetime.now(MSK)
    
    if start.tzinfo is None:
        start = MSK.localize(start)
    if end.tzinfo is None:
        end = MSK.localize(end)
    
    start_ts = int(start.timestamp())
    end_ts = int(end.timestamp())
    
    duration = (end.date() - start.date()).days
    days_word = pluralize_days(duration)
    
    result = f"Даты: <t:{start_ts}:d> - <t:{end_ts}:d> ({duration} {days_word})"
    
    if current_time < start:
        result += f"\nНачнется: <t:{start_ts}:R>"
    elif current_time <= end:
        result += f"\nЗакончится: <t:{end_ts}:R>"
    
    return result


CLAN_MEMBERS_CACHE = []
CLAN_MEMBERS_CACHE_TIME = None
CLAN_MEMBERS_CACHE_TTL = 3600
CLAN_MEMBER_CREATED_AT_CACHE = {}


def get_member_created_at(nickname: str):
    return CLAN_MEMBER_CREATED_AT_CACHE.get(nickname)


def invalidate_clan_members_cache():
    global CLAN_MEMBERS_CACHE_TIME
    CLAN_MEMBERS_CACHE_TIME = None


def _parse_firestore_dt(value):
    """Приводит значение Firestore Timestamp к datetime с tzinfo, либо None."""
    if isinstance(value, datetime):
        return value if value.tzinfo else pytz.UTC.localize(value)
    return None

_ROSTER_UNAVAILABLE_WARNED = False


VOICE_ROOMS = {}
TRIGGER_CHANNEL_ARMY = None
TRIGGER_CHANNEL_PUBLIC = None
# Блокировки по member.id для защиты от двойного создания временных комнат
VOICE_ROOM_CREATION_LOCKS = {}

VACATION_RULES = es("""

Боец, если ты будешь отсутствовать более 7 дней, оформи отпуск, чтобы не быть исключённым из клана за отсутствие отметок на мероприятиях с обязательной записью!

**📌 Основные правила:**
* Отпуск оформляется на срок от **7 дней до 1 месяца с обязательным указанием причины**
* Отпуск **можно продлить**, создав новый со следующего дня после окончания предыдущего
* Отпуск **можно досрочно закрыть** в любое время
* После оформления отпуск должен быть **утверждён комбатом или заместителем**
* Во время отпуска тебе **не нужно отмечаться в расписании на матчи**
* Боец в отпуске **лишается возможности участия в матчах** до закрытия отпуска

**✏️ Причины:**
* Командировки и мероприятия по работе
* Семейные мероприятия
* Проблемы со здоровьем
* Длительные учебные мероприятия (например, сессия)
* Отдых от игры, игровое выгорание
* Длительное моральное, физическое утомление

Боец, указывай честную и конкретную причину! Это помогает командованию планировать состав на матчи.
Учти, что если ты будешь постоянно оформлять отпуски для отдыха от игры и никогда не участвовать в мероприятиях клана, это может стать поводом для понижения в клане – вплоть до исключения, но с возможностью восстановления.
""")

# ============== ИНИЦИАЛИЗАЦИЯ ==============

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

client = discord.Client(intents=intents)
scheduler = AsyncIOScheduler(timezone=MSK)

check_lock = asyncio.Lock()


class MessageDeduplicator:
    def __init__(self, maxlen=500):
        self._order = deque(maxlen=maxlen)
        self._seen = set()

    def mark_processed(self, message_id: int) -> bool:
        if message_id in self._seen:
            return True
        if len(self._order) == self._order.maxlen:
            oldest = self._order[0]
            self._seen.discard(oldest)
        self._order.append(message_id)
        self._seen.add(message_id)
        return False


dedup = MessageDeduplicator(maxlen=500)

try:
    gc = gspread.service_account(filename=GOOGLE_CREDENTIALS_FILE)
except Exception as e:
    print(f"Ошибка при инициализации Google Sheets: {e}")
    gc = None

if USE_FIREBASE_BACKEND:
    try:
        if not firebase_admin._apps:
            _fb_cred = firebase_credentials.Certificate(FIREBASE_CREDENTIALS_FILE)
            firebase_admin.initialize_app(_fb_cred, {'projectId': FIREBASE_PROJECT_ID})
        fs_db = firestore.client()
        print("✅ Firebase Admin SDK инициализирован")
    except Exception as e:
        print(f"❌ Ошибка при инициализации Firebase: {e}")
        fs_db = None
else:
    fs_db = None
    print("ℹ️ DATA_BACKEND='json' — подключение к Firebase пропущено (работаем только с локальными JSON-файлами).")


# ============== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==============

def extract_nickname(raw_text):
    raw_text = raw_text.strip()
    if len(raw_text) <= 35:
        return raw_text
    match_paren = re.search(r'\(["\']?(.*?)["\']?\)', raw_text)
    if match_paren:
        extracted = match_paren.group(1).strip()
        if len(extracted) <= 35:
            return extracted
    match_bracket = re.search(r'(\[.*?\])', raw_text)
    if match_bracket:
        extracted = match_bracket.group(1).strip()
        if len(extracted) <= 35:
            return extracted
    return None


def get_color_category(bg):
    if not bg:
        return None
    r = bg.get('red', 0)
    g = bg.get('green', 0)
    b = bg.get('blue', 0)
    if 0.85 < r < 0.98 and 0.50 < g < 0.70 and 0.50 < b < 0.70:
        if r > g and r > b:
            return 'red'
    if 0.90 < r <= 1.0 and 0.80 < g < 0.95 and 0.50 < b < 0.70:
        if r > g and g > b:
            return 'yellow'
    return None


def get_sheet_data_with_colors_sync(sheet, range_name):
    """Синхронная версия для выполнения в executor. Выбрасывает исключения при ошибках."""
    base_url = 'https://sheets.googleapis.com/v4/spreadsheets'
    full_url = f'{base_url}/{sheet.spreadsheet.id}'
    range_str = f"'{sheet.title}'!{range_name}"
    params = {
        'ranges': range_str,
        'includeGridData': 'true',
        'fields': 'sheets.data.rowData.values(effectiveFormat/backgroundColor,userEnteredValue)'
    }
    res = sheet.client.request('get', full_url, params=params)
    data = res.json()
    rows_data = data['sheets'][0]['data'][0].get('rowData', [])
    result = []
    for row in rows_data:
        row_result = []
        for cell in row.get('values', []):
            val_obj = cell.get('userEnteredValue', {})
            val = str(val_obj.get('stringValue', val_obj.get('numberValue', val_obj.get('boolValue', ''))))
            bg = cell.get('effectiveFormat', {}).get('backgroundColor', None)
            row_result.append({'value': val, 'bg': bg})
        result.append(row_result)
    return result


async def get_sheet_data_with_colors(sheet, range_name):
    """Асинхронная обёртка с retry для Google API."""
    max_retries = 3
    last_error = None
    for attempt in range(max_retries):
        try:
            return await asyncio.get_event_loop().run_in_executor(
                EXECUTOR, get_sheet_data_with_colors_sync, sheet, range_name
            )
        except Exception as e:
            last_error = e
            error_str = str(e)
            if ('503' in error_str or '500' in error_str or '429' in error_str) and attempt < max_retries - 1:
                wait_time = 2 * (attempt + 1)
                print(f"⚠️ Google API ошибка (попытка {attempt + 1}/{max_retries}), повтор через {wait_time} сек...")
                await asyncio.sleep(wait_time)
                continue
            break
    print(f"❌ Ошибка при получении данных с цветами из API: {last_error}")
    return []

def _firebase_load_roster_sync():
    """Синхронное чтение состава клана из коллекции 'profiles' (Admin SDK
    обходит правила безопасности — читать всю коллекцию для бота безопасно,
    хотя клиентским приложениям сайта это запрещено правилами Firestore).
    Возвращает (members, created_at_map) — members содержит ТОЛЬКО игроков
    с composition 'Личный состав'/'Запас', created_at_map — дату регистрации
    ВСЕХ найденных профилей (нужна для фильтрации 'Не отметились' у старых
    мероприятий, см. get_active_members)."""
    docs = fs_db.collection('profiles').stream()
    members = []
    created_at_map = {}
    skipped = 0
    for doc in docs:
        data = doc.to_dict() or {}
        callsign = (data.get('callsign') or '').strip()
        if not callsign:
            continue
        nickname = f"{CLAN_TAG}{callsign}"
        created_at_dt = _parse_firestore_dt(data.get('createdAt'))
        if created_at_dt:
            created_at_map[nickname] = created_at_dt
        composition = ((data.get('gameRoles') or {}).get(CLAN_ROSTER_GAME) or {}).get('composition', '')
        if composition not in ACTIVE_CLAN_COMPOSITIONS:
            skipped += 1
            continue
        members.append(nickname)
    if skipped:
        print(f"ℹ️ Пропущено {skipped} профилей из-за неподходящего состава (не 'Личный состав'/'Запас')")
    return members, created_at_map


async def load_clan_members_from_firebase():
    """Список бойцов клана из Firebase (rosterPublic) — используется для явки,
    гейта на участие в мероприятиях и списка 'активных бойцов'.
    НЕ используется для check_spreadsheet() — та проверка ошибок регистрации
    по-прежнему читает исходную Google-таблицу напрямую, без изменений."""
    global CLAN_MEMBERS_CACHE, CLAN_MEMBERS_CACHE_TIME
    current_time = datetime.now().timestamp()
    if CLAN_MEMBERS_CACHE and CLAN_MEMBERS_CACHE_TIME and (current_time - CLAN_MEMBERS_CACHE_TIME) < CLAN_MEMBERS_CACHE_TTL:
        return CLAN_MEMBERS_CACHE
    global _ROSTER_UNAVAILABLE_WARNED
    if not fs_db:
        if not _ROSTER_UNAVAILABLE_WARNED:
            print("⚠️ Firebase недоступен — список бойцов клана из Firebase получить нельзя "
                  "(нормально в режиме DATA_BACKEND='json'). Дальнейшие такие сообщения подавлены.")
            _ROSTER_UNAVAILABLE_WARNED = True
        return CLAN_MEMBERS_CACHE
    try:
        loop = asyncio.get_event_loop()
        members, created_at_map = await loop.run_in_executor(EXECUTOR, _firebase_load_roster_sync)
        CLAN_MEMBERS_CACHE = members
        CLAN_MEMBERS_CACHE_TIME = current_time
        global CLAN_MEMBER_CREATED_AT_CACHE
        CLAN_MEMBER_CREATED_AT_CACHE = created_at_map
        print(f"✅ Загружено {len(members)} участников клана из Firebase")
        return members
    except Exception as e:
        print(f"❌ Ошибка загрузки списка клана из Firebase: {e}")
        return CLAN_MEMBERS_CACHE

QUEUE_CACHE = {'current': None, 'time': 0}
QUEUE_CACHE_TTL = 300

UID_CALLSIGN_CACHE = {}
UID_CALLSIGN_CACHE_TIME = {}
UID_CALLSIGN_CACHE_TTL = 3600


def _firebase_read_queue_sync():
    doc = fs_db.collection('queue').document('state').get()
    if not doc.exists:
        return []
    return (doc.to_dict() or {}).get('current', []) or []


def _firebase_read_profile_callsign_sync(uid):
    doc = fs_db.collection('profiles').document(uid).get()
    if not doc.exists:
        return None
    return ((doc.to_dict() or {}).get('callsign') or '').strip() or None


async def get_uid_callsign(uid: str):
    now = datetime.now().timestamp()
    cached_time = UID_CALLSIGN_CACHE_TIME.get(uid, 0)
    if uid in UID_CALLSIGN_CACHE and (now - cached_time) < UID_CALLSIGN_CACHE_TTL:
        return UID_CALLSIGN_CACHE[uid]
    if not fs_db:
        return UID_CALLSIGN_CACHE.get(uid)
    try:
        loop = asyncio.get_event_loop()
        callsign = await loop.run_in_executor(EXECUTOR, _firebase_read_profile_callsign_sync, uid)
        UID_CALLSIGN_CACHE[uid] = callsign
        UID_CALLSIGN_CACHE_TIME[uid] = now
        return callsign
    except Exception as e:
        print(f"⚠️ Не удалось получить callsign для uid {uid}: {e}")
        return UID_CALLSIGN_CACHE.get(uid)


async def get_commander_queue():
    now = datetime.now().timestamp()
    if QUEUE_CACHE['current'] is not None and (now - QUEUE_CACHE['time']) < QUEUE_CACHE_TTL:
        return QUEUE_CACHE['current']
    if not fs_db:
        return QUEUE_CACHE['current'] or []
    try:
        loop = asyncio.get_event_loop()
        current = await loop.run_in_executor(EXECUTOR, _firebase_read_queue_sync)
        QUEUE_CACHE['current'] = current
        QUEUE_CACHE['time'] = now
        return current
    except Exception as e:
        print(f"⚠️ Не удалось получить очередь на командование: {e}")
        return QUEUE_CACHE['current'] or []


async def get_expected_squad_commander(event: dict, current_date: datetime):
    """Следующий в очереди на командование отделением (Firebase queue/state),
    пропуская тех, кто в отпуске или явно отказался от участия в мероприятии."""
    queue = await get_commander_queue()
    declined = event.get('declined', {})
    for entry in queue:
        uid = entry.get('uid')
        if not uid:
            continue
        callsign = await get_uid_callsign(uid)
        if not callsign:
            continue
        nickname = f"{CLAN_TAG}{callsign}"
        if nickname in declined:
            continue
        if is_on_vacation_dynamic(nickname, current_date):
            continue
        return nickname
    return None

async def find_discord_user(nickname: str, thread):
    try:
        guild = thread.guild
        for member in guild.members:
            if member.display_name == nickname:
                return member
        for member in guild.members:
            if nickname.lower() in member.display_name.lower():
                return member
        return None
    except Exception as e:
        print(f"Ошибка при поиске пользователя {nickname}: {e}")
        return None


async def find_member_by_nickname(nickname: str):
    try:
        channel = await client.fetch_channel(VACATION_CHANNEL_ID)
        guild = channel.guild
        for member in guild.members:
            if member.display_name == nickname:
                return member
        for member in guild.members:
            if nickname.lower() in member.display_name.lower():
                return member
        return None
    except Exception as e:
        print(f"Ошибка поиска участника: {e}")
        return None


async def send_chunked(thread, text, user_name="") -> list:
    """Возвращает список ID отправленных сообщений (нужно для последующего удаления)."""
    ids = []
    if not text:
        return ids
    chunk_size = 1800
    if len(text) <= chunk_size:
        msg = await thread.send(text)
        ids.append(msg.id)
        await asyncio.sleep(0.5)
        return ids
    chunks = [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]
    for chunk in chunks:
        try:
            msg = await thread.send(chunk)
            ids.append(msg.id)
            await asyncio.sleep(0.5)
        except Exception as e:
            print(f"Ошибка при отправке части: {e}")
            raise
    return ids


# ============== ПОСТРОЕНИЕ ТЕКСТОВ ==============

def build_intro_lines(current_time: datetime) -> list:
    return [
        es("🔔 **Проверка бойцов**"), "",
        es("Это автоматическая проверка бойцов на **соответствие требованиям членства в клане** — всех, кто не в отпуске. "),
        es("Игнорирование результатов проверки приведет к **автоматическому назначению выговора**."),
        es("🔴 **Красные** проблемы — критические, требуют немедленного исправления."),
        es("🟡 **Желтые** проблемы — некритические, но важные проблемы, которые тоже требуют исправления."),
        es(f"📅 Проверка от {current_time.strftime('%d.%m.%Y %H:%M')} МСК"), " ",
    ]


def build_intro_message(current_time: datetime) -> str:
    return "\n".join(build_intro_lines(current_time))


def build_user_message(discord_user, issues: list) -> str:
    red_issues = [i for i in issues if i['severity'] == 'red']
    yellow_issues = [i for i in issues if i['severity'] == 'yellow']
    parts = [f"👤 **{discord_user.mention}**", ""]
    def issue_line(issue):
        text = issue['text'].strip()
        if not text:
            text = f"({issue['column']})"
        return f"* {text}"
    if red_issues:
        parts.append(es("🔴 **Критические проблемы:**"))
        for issue in red_issues:
            parts.append(issue_line(issue))
        parts.append("")
    if yellow_issues:
        parts.append(es("🟡 **Важные проблемы:**"))
        for issue in yellow_issues:
            parts.append(issue_line(issue))
        parts.append("")
    return "\n".join(parts).strip("\n")


async def check_spreadsheet():
    if check_lock.locked():
        return
    async with check_lock:
        try:
            thread = await client.fetch_channel(THREAD_ID)

            # Удаляем сообщения ПРЕДЫДУЩЕЙ проверки перед публикацией новой —
            # иначе в ветке копятся устаревшие отчёты о проблемах, которые
            # бойцы могли уже исправить.
            prev = load_json(CHECK_MESSAGES_FILE, {})
            for msg_id in prev.get('message_ids', []):
                try:
                    old_msg = await thread.fetch_message(msg_id)
                    await old_msg.delete()
                except Exception:
                    pass
            save_json(CHECK_MESSAGES_FILE, {'message_ids': [], 'thread_id': THREAD_ID})

            if not gc:
                return
            loop = asyncio.get_event_loop()
            spreadsheet = await loop.run_in_executor(EXECUTOR, gc.open_by_url, SPREADSHEET_URL)
            sheet = spreadsheet.worksheet(SHEET_NAME)
            data_with_colors = await get_sheet_data_with_colors(sheet, 'A1:J35')
            if not data_with_colors or len(data_with_colors) < 2:
                return
            headers = [cell['value'] for cell in data_with_colors[0]]
            rows = data_with_colors[1:]
            current_time = datetime.now(MSK)
            user_issues = {}
            users_not_found = []
            for row in rows:
                raw_nickname = ''
                if NICKNAME_COLUMN in headers:
                    nick_idx = headers.index(NICKNAME_COLUMN)
                    if nick_idx < len(row):
                        raw_nickname = row[nick_idx]['value'].strip()
                nickname = extract_nickname(raw_nickname)
                if not nickname:
                    continue
                if is_on_vacation_dynamic(nickname, current_time):
                    continue
                issues = []
                for col_name in COLUMNS_TO_CHECK:
                    if col_name in headers:
                        col_idx = headers.index(col_name)
                        if col_idx < len(row):
                            cell_data = row[col_idx]
                            color = get_color_category(cell_data['bg'])
                            if color in ['red', 'yellow']:
                                issues.append({'column': col_name, 'text': cell_data['value'].strip(), 'severity': color})
                if issues:
                    discord_user = await find_discord_user(nickname, thread)
                    if discord_user:
                        user_issues[discord_user] = issues
                    else:
                        users_not_found.append(nickname)

            new_message_ids = []
            if user_issues or users_not_found:
                intro = build_intro_message(current_time)
                if len(intro) <= EXPECTED_INTRO_MAX_LEN:
                    new_message_ids += await send_chunked(thread, intro, "вводное сообщение")
                for discord_user, issues in user_issues.items():
                    user_msg = build_user_message(discord_user, issues)
                    new_message_ids += await send_chunked(thread, user_msg, discord_user.display_name)
                if users_not_found:
                    not_found_msg = ("\n\n" + es("⚠️ **Не удалось найти в Discord:**\n") + ", ".join(users_not_found))
                    new_message_ids += await send_chunked(thread, not_found_msg, "список ненайденных")

            save_json(CHECK_MESSAGES_FILE, {'message_ids': new_message_ids, 'thread_id': THREAD_ID})
        except Exception as e:
            print(f"Ошибка при проверке: {e}")

async def scheduled_check_spreadsheet():
    """Обёртка над check_spreadsheet исключительно для планировщика.
    Защищает от повторного постинга, если по какой-то причине окажется
    запущено больше одного процесса бота одновременно."""
    now = datetime.now(MSK)
    slot_key = now.strftime('%Y-%m-%d %H:%M')
    last = load_json(LAST_SCHEDULED_CHECK_FILE, {})
    if last.get('slot') == slot_key:
        print(f"⚠️ Плановая проверка для {slot_key} уже выполнена (PID {last.get('pid')}), пропускаю дубль.")
        return
    save_json(LAST_SCHEDULED_CHECK_FILE, {'slot': slot_key, 'pid': os.getpid(), 'at': now.isoformat()})
    await check_spreadsheet()

# ============== РАБОТА С ДАННЫМИ ==============

_FIRESTORE_CACHE = {}
_FIRESTORE_CACHE_LOCK = threading.Lock()

BACKUP_SAFETY_DIR = os.path.join(BASE_DIR, 'backups')


def _firestore_doc_ref(doc_name):
    return fs_db.collection('botData').document(doc_name)


def _firestore_read_sync(doc_name):
    """Синхронное чтение (выполняется в EXECUTOR, не блокирует event loop)."""
    doc_ref = _firestore_doc_ref(doc_name)
    snap = doc_ref.get()
    if snap.exists:
        return (snap.to_dict() or {}).get('data', {})
    return {}


def _firestore_write_sync(doc_name, data):
    """Синхронная запись (выполняется в EXECUTOR, не блокирует event loop)."""
    if not fs_db:
        return
    try:
        doc_ref = _firestore_doc_ref(doc_name)
        doc_ref.set({'data': data, 'updatedAt': firestore.SERVER_TIMESTAMP})
    except Exception as e:
        print(f"❌ Ошибка записи в Firebase ({doc_name}): {e}")


def _write_local_backup_sync(filename, data):
    """Пишет резервную JSON-копию на диск. В режиме DATA_BACKEND='firebase'
    вызывается при КАЖДОЙ записи (зеркалирование) — бэкапы всегда идентичны
    тому, что лежит в Firebase, на случай аварийного переключения на них."""
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"⚠️ Не удалось записать резервную копию '{filename}': {e}")


def _read_local_backup_sync(filename, default):
    if not os.path.exists(filename):
        return default
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Не удалось прочитать резервную копию '{filename}': {e}")
        return default


def _data_size(data) -> int:
    if isinstance(data, dict):
        return len(data)
    if isinstance(data, list):
        return len(data)
    return 0 if data in (None, {}, []) else 1


def _is_suspicious_wipe(old_data, new_data) -> bool:
    """Эвристика 'подозрительного' массового стирания: было заметное
    количество записей, стало пусто (или почти пусто) за один вызов.
    НЕ блокирует запись (админ мог реально удалить всё намеренно —
    например, удалить последнее мероприятие), но включает создание
    защитного снапшота ПЕРЕД перезаписью, чтобы данные можно было
    восстановить вручную при ошибке. Порог old_size >= 3 нужен, чтобы
    не создавать лишний шум на естественных 'опустошениях' вроде
    закрытия последней временной голосовой комнаты (0-2 штуки — норма)."""
    old_size = _data_size(old_data)
    new_size = _data_size(new_data)
    if old_size < 3:
        return False
    if new_size == 0:
        return True
    if new_size < old_size * 0.2:
        return True
    return False


def _save_wipe_safety_snapshot_sync(doc_name, old_data):
    """Сохраняет 'слепок' данных ПЕРЕД подозрительным массовым стиранием —
    и в отдельную коллекцию Firebase (botData_safety), и локально на диск
    (backups/{doc_name}_wipe_TIMESTAMP.json). Это чисто аварийная копия
    для ручного восстановления, в обычном чтении она не участвует."""
    timestamp = datetime.now(MSK).strftime('%Y%m%d_%H%M%S')
    if fs_db:
        try:
            fs_db.collection('botData_safety').document(f"{doc_name}_{timestamp}").set({
                'data': old_data, 'savedAt': firestore.SERVER_TIMESTAMP, 'reason': 'suspicious_wipe'
            })
        except Exception as e:
            print(f"⚠️ Не удалось сохранить защитный снапшот в Firebase для '{doc_name}': {e}")
    try:
        os.makedirs(BACKUP_SAFETY_DIR, exist_ok=True)
        snapshot_path = os.path.join(BACKUP_SAFETY_DIR, f"{doc_name}_wipe_{timestamp}.json")
        with open(snapshot_path, 'w', encoding='utf-8') as f:
            json.dump(old_data, f, indent=2, ensure_ascii=False)
        print(f"🛟 Обнаружено подозрительное массовое удаление данных в '{doc_name}' — "
              f"создан защитный снапшот: {snapshot_path}")
    except Exception as e:
        print(f"⚠️ Не удалось сохранить локальный защитный снапшот для '{doc_name}': {e}")


async def load_all_firebase_data():
    """Загружает все данные бота из Firebase в память при старте
    (только в режиме DATA_BACKEND='firebase'). Если чтение конкретной
    коллекции не удалось (сетевая ошибка и т.п.) — НЕ считаем её пустой,
    а подстраховываемся локальным бэкап-файлом (если он есть), чтобы
    не потерять реальные данные и случайно не затереть их где-то ниже."""
    if not USE_FIREBASE_BACKEND:
        print("ℹ️ DATA_BACKEND='json' — работаю напрямую с локальными JSON-файлами, Firebase не используется.")
        return
    if not fs_db:
        print("⚠️ Firebase не инициализирован — данные бота НЕ будут сохраняться в облако!")
        return
    loop = asyncio.get_event_loop()
    for local_name, doc_name in FIREBASE_DATA_MAP.items():
        try:
            data = await loop.run_in_executor(EXECUTOR, _firestore_read_sync, doc_name)
            with _FIRESTORE_CACHE_LOCK:
                _FIRESTORE_CACHE[local_name] = data
            # Зеркалируем актуальное состояние Firebase в локальный бэкап,
            # чтобы бэкап был свежим даже если сегодня никто ничего не сохранял.
            await loop.run_in_executor(EXECUTOR, _write_local_backup_sync, local_name, data)
        except Exception as e:
            print(f"❌ Ошибка загрузки '{doc_name}' из Firebase: {e}")
            fallback = await loop.run_in_executor(EXECUTOR, _read_local_backup_sync, local_name, None)
            if fallback is not None:
                print(f"🛟 Использую локальный бэкап '{local_name}' вместо недоступных данных Firebase.")
                with _FIRESTORE_CACHE_LOCK:
                    _FIRESTORE_CACHE[local_name] = fallback
            else:
                print(f"⚠️ Локальный бэкап для '{local_name}' тоже не найден — коллекция будет считаться пустой.")
                with _FIRESTORE_CACHE_LOCK:
                    _FIRESTORE_CACHE[local_name] = {}
    print(f"✅ Данные бота загружены из Firebase ({len(FIREBASE_DATA_MAP)} коллекций), локальные бэкапы синхронизированы.")


def load_json(filename, default=None):
    if default is None:
        default = {}
    if filename not in FIREBASE_DATA_MAP:
        if not os.path.exists(filename):
            return default
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return default

    if not USE_FIREBASE_BACKEND:
        # РЕЖИМ JSON: читаем напрямую с диска при каждом вызове, без кэша.
        # Firebase не используется вообще.
        return _read_local_backup_sync(filename, default)

    # РЕЖИМ FIREBASE: читаем ТОЛЬКО из памяти (кэш, загруженный из Firebase
    # при старте и обновляемый при каждой записи) — на диск даже не смотрим.
    with _FIRESTORE_CACHE_LOCK:
        cached = _FIRESTORE_CACHE.get(filename)
    if cached is None:
        return default
    return copy.deepcopy(cached)


def save_json(filename, data):
    if filename not in FIREBASE_DATA_MAP:
        try:
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"Ошибка сохранения {filename}: {e}")
        return

    if not USE_FIREBASE_BACKEND:
        # РЕЖИМ JSON: пишем напрямую на диск, Firebase не трогаем вообще.
        _write_local_backup_sync(filename, data)
        return

    # РЕЖИМ FIREBASE: сразу обновляем кэш (для мгновенной консистентности
    # внутри процесса), затем в фоне (через EXECUTOR, не блокируя event loop)
    # пишем и в Firebase, и зеркальный бэкап на диск.
    doc_name = FIREBASE_DATA_MAP[filename]
    with _FIRESTORE_CACHE_LOCK:
        old_data = _FIRESTORE_CACHE.get(filename)
        _FIRESTORE_CACHE[filename] = copy.deepcopy(data)

    if _is_suspicious_wipe(old_data, data):
        try:
            EXECUTOR.submit(_save_wipe_safety_snapshot_sync, doc_name, old_data)
        except Exception as e:
            print(f"❌ Не удалось запланировать защитный снапшот для '{doc_name}': {e}")

    if fs_db:
        try:
            EXECUTOR.submit(_firestore_write_sync, doc_name, data)
        except Exception as e:
            print(f"❌ Не удалось запланировать запись в Firebase ({doc_name}): {e}")

    try:
        EXECUTOR.submit(_write_local_backup_sync, filename, data)
    except Exception as e:
        print(f"⚠️ Не удалось запланировать резервную запись '{filename}': {e}")


def is_on_vacation_dynamic(nickname: str, current_date: datetime) -> bool:
    vacations = load_json(VACATIONS_FILE, {})
    clean_nickname = nickname.strip().lower()
    current_date_only = current_date.date()
    for vac_name, data in vacations.items():
        if clean_nickname == vac_name.strip().lower():
            if data.get('status') != 'active':
                continue
            try:
                start = datetime.fromisoformat(data['start']).date()
                end = datetime.fromisoformat(data['end']).date()
                if start <= current_date_only <= end:
                    return True
            except Exception:
                continue
    return False


async def get_active_members(current_date: datetime, registered_before: datetime = None) -> list:
    """Список активных (не в отпуске) бойцов. Если передан registered_before —
    дополнительно исключает бойцов, ещё не зарегистрированных на сайте на этот
    момент (используется для 'Не отметились' у СТАРЫХ мероприятий)."""
    members = await load_clan_members_from_firebase()
    filtered = [m for m in members if not is_on_vacation_dynamic(m, current_date)]
    if registered_before is not None:
        filtered = [
            m for m in filtered
            if get_member_created_at(m) is None or get_member_created_at(m) <= registered_before
        ]
    return filtered


async def build_mentions_for_nicknames(nicknames: list) -> str:
    """Строит через пробел Discord-упоминания игроков по их позывным
    (запасной вариант — жирный текст ника, если пользователь Discord не найден)."""
    mentions = []
    for nickname in nicknames:
        member = await find_member_by_nickname(nickname)
        mentions.append(member.mention if member else f"**{nickname}**")
    return " ".join(mentions)


async def get_all_active_members_mentions(current_time: datetime) -> str:
    """Упоминания ВСЕХ действующих бойцов клана (composition из
    ACTIVE_CLAN_COMPOSITIONS), которые НЕ в отпуске на момент вызова.
    Используется везде, где раньше пинговалась роль 'Боец ArmA' целиком —
    теперь эта роль нигде в коде не пингуется вообще."""
    active_members = await get_active_members(current_time)
    return await build_mentions_for_nicknames(active_members)


async def get_vacation_role(guild):
    role = guild.get_role(ROLE_IDS['otpusk'])
    if role:
        return role
    # Фоллбэк на случай, если роль с этим ID вдруг не найдена на сервере
    # (например, роль была удалена) — создаём заново по старому принципу.
    try:
        return await guild.create_role(name="Отпуск", mentionable=False)
    except Exception:
        return None


async def update_vacation_role(member, has_vacation: bool):
    try:
        guild = member.guild
        role = await get_vacation_role(guild)
        if not role:
            return
        if has_vacation:
            if role not in member.roles:
                await member.add_roles(role)
        else:
            if role in member.roles:
                await member.remove_roles(role)
    except Exception as e:
        print(f"❌ Ошибка обновления роли: {e}")


# ============== UI КОМПОНЕНТЫ ==============

# ============== ЯКОРНЫЕ СООБЩЕНИЯ АДМИН-КАНАЛА ==============
# Три постоянных сообщения в ADMIN_CHANNEL_ID: панель управления, уведомления
# (с сайта) и логирование. У двух последних есть собственные ветки — именно
# в них публикуются новые уведомления/логи. ID всех трёх сообщений и обеих
# веток хранятся в ADMIN_ANCHORS_FILE, чтобы бот находил их же при рестарте,
# а не плодил дубликаты.

ANCHOR_EMBED_COLOR = discord.Color.blue()

ADMIN_PANEL_DESCRIPTION = (
    "Здесь вы можете управлять всеми функциями бота через кнопки. "
    "Функции бота разделены по строчкам:\n\n"
    "1. Единоразовые мероприятия \n2. Еженедельные мероприятия \n3. Сообщения \n4. Отпуска \n5. Утилиты\n"
)

NOTIFICATIONS_ANCHOR_DESCRIPTION = (
    "Здесь в ветке публикуются все уведомления с сайта для игроков — "
    "дисциплинарные взыскания, зачтённые отыгрыши и т.п."
)

LOGGING_ANCHOR_DESCRIPTION = (
    "Здесь в ветке публикуются все логи бота — для диагностики проблем без необходимости "
    "подключаться к серверу напрямую."
)


BOT_STARTED_AT = datetime.now(MSK)

def build_admin_panel_embed():
    embed = discord.Embed(title=es("🛠️ Панель управления комбата и заместителей"),
                           description=ADMIN_PANEL_DESCRIPTION, color=ANCHOR_EMBED_COLOR)
    started_str = BOT_STARTED_AT.strftime('%d.%m.%Y %H:%M')
    embed.set_footer(text=f"Последний запуск бота: {started_str} МСК")
    return embed


def build_notifications_anchor_embed():
    return discord.Embed(title=es("ℹ️ Уведомления"),
                          description=NOTIFICATIONS_ANCHOR_DESCRIPTION, color=ANCHOR_EMBED_COLOR)


def build_logging_anchor_embed():
    return discord.Embed(title=es("🔧 Логирование"),
                          description=LOGGING_ANCHOR_DESCRIPTION, color=ANCHOR_EMBED_COLOR)


async def _find_or_create_anchor(channel, title: str, embed_builder, thread_name: str = None, view=None):
    """Ищет среди последних сообщений бота в канале embed с данным title.
    Если не находит — создаёт новое сообщение (+ ветку, если thread_name задан).
    Возвращает (message, thread_or_None)."""
    async for message in channel.history(limit=50):
        if message.author.id != client.user.id:
            continue
        if message.embeds and message.embeds[0].title == title:
            thread = None
            if thread_name and message.thread:
                thread = message.thread
            elif thread_name:
                try:
                    thread = await message.create_thread(name=thread_name)
                except Exception:
                    thread = None
            return message, thread

    message = await channel.send(embed=embed_builder(), view=view)
    thread = None
    if thread_name:
        try:
            thread = await message.create_thread(name=thread_name)
        except Exception as e:
            print(f"⚠️ Не удалось создать ветку '{thread_name}': {e}")
    return message, thread


async def ensure_admin_channel_anchors():
    """Находит (или создаёт при самом первом запуске) три якорных сообщения
    в ADMIN_CHANNEL_ID. Панель управления ОБНОВЛЯЕТСЯ при каждом старте бота
    (актуализация embed+view); уведомления/логирование — только создаются
    один раз, их оформление актуализируется через синхронизацию шаблонов."""
    channel = await client.fetch_channel(ADMIN_CHANNEL_ID)
    anchors = load_json(ADMIN_ANCHORS_FILE, {})

    panel_msg, _ = await _find_or_create_anchor(channel, es("🛠️ Панель управления комбата и заместителей"),
                                                 build_admin_panel_embed, thread_name=None, view=AdminMainMenuView())
    try:
        await panel_msg.edit(embed=build_admin_panel_embed(), view=AdminMainMenuView())
    except Exception as e:
        print(f"⚠️ Не удалось обновить панель управления: {e}")
    anchors['panel_message_id'] = panel_msg.id

    notif_msg, notif_thread = await _find_or_create_anchor(channel, es("ℹ️ Уведомления"),
                                                            build_notifications_anchor_embed, thread_name="ℹ️ Уведомления")
    anchors['notifications_message_id'] = notif_msg.id
    if notif_thread:
        anchors['notifications_thread_id'] = notif_thread.id

    log_msg, log_thread = await _find_or_create_anchor(channel, es("🔧 Логирование"),
                                                         build_logging_anchor_embed, thread_name="🔧 Логирование")
    anchors['logging_message_id'] = log_msg.id
    if log_thread:
        anchors['logging_thread_id'] = log_thread.id

    save_json(ADMIN_ANCHORS_FILE, anchors)
    print(f"✅ Якорные сообщения админ-канала готовы (панель, уведомления, логирование)")


async def get_notifications_thread_id():
    anchors = load_json(ADMIN_ANCHORS_FILE, {})
    return anchors.get('notifications_thread_id')


async def get_logging_thread_id():
    anchors = load_json(ADMIN_ANCHORS_FILE, {})
    return anchors.get('logging_thread_id')

class VacationModal(discord.ui.Modal, title=es("🏖️ Оформление отпуска")):
    start_date = discord.ui.TextInput(label="Дата начала (ДД.ММ.ГГГГ)", placeholder="15.08.2026", required=True, max_length=10)
    end_date = discord.ui.TextInput(label="Дата окончания (ДД.ММ.ГГГГ)", placeholder="22.08.2026", required=True, max_length=10)
    reason = discord.ui.TextInput(label="Причина отпуска", style=discord.TextStyle.paragraph, required=True, max_length=500)
    async def on_submit(self, interaction):
        await handle_vacation_request(interaction, interaction.user.display_name, self.start_date.value, self.end_date.value, self.reason.value, by_admin=False)


class AdminVacationModal(discord.ui.Modal, title=es("🏖️ Отпуск для бойца (комбат)")):
    player_name = discord.ui.TextInput(label="Позывной бойца с клантегом", placeholder="[En-Y]Killa", required=True, max_length=35)
    start_date = discord.ui.TextInput(label="Дата начала (ДД.ММ.ГГГГ)", placeholder="15.08.2026", required=True, max_length=10)
    end_date = discord.ui.TextInput(label="Дата окончания (ДД.ММ.ГГГГ)", placeholder="22.08.2026", required=True, max_length=10)
    reason = discord.ui.TextInput(label="Причина отпуска", style=discord.TextStyle.paragraph, required=True, max_length=500)
    async def on_submit(self, interaction):
        await handle_vacation_request(interaction, self.player_name.value, self.start_date.value, self.end_date.value, self.reason.value, by_admin=True)


class SendMessageModal(discord.ui.Modal, title=es("📝 Отправка сообщения")):
    channel_id = discord.ui.TextInput(label="ID канала или ветки", required=True, max_length=20)
    message_text = discord.ui.TextInput(label="Текст сообщения", style=discord.TextStyle.paragraph, required=True, max_length=2000)
    async def on_submit(self, interaction):
        try:
            channel = await client.fetch_channel(int(self.channel_id.value))
            await channel.send(self.message_text.value)
            await interaction.response.send_message(es("✅ Сообщение отправлено!"), ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)


class DeleteMessageModal(discord.ui.Modal, title=es("🗑️ Удаление сообщения")):
    channel_id = discord.ui.TextInput(label="ID канала или ветки", required=True, max_length=20)
    message_id = discord.ui.TextInput(label="ID сообщения", required=True, max_length=20)
    async def on_submit(self, interaction):
        try:
            channel = await client.fetch_channel(int(self.channel_id.value))
            message = await channel.fetch_message(int(self.message_id.value))
            await message.delete()
            await interaction.response.send_message(es("✅ Сообщение удалено!"), ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)


class ExtractMessageModal(discord.ui.Modal, title=es("🔍 Извлечь код сообщения")):
    channel_id = discord.ui.TextInput(label="ID канала или ветки", required=True, max_length=20)
    message_id = discord.ui.TextInput(label="ID сообщения", required=True, max_length=20)
    async def on_submit(self, interaction):
        try:
            await extract_message_structure(interaction, int(self.channel_id.value), int(self.message_id.value))
        except Exception as e:
            await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)


class EventCreateModal(discord.ui.Modal):
    def __init__(self, image_key='none', num_games=0, mandatory=True):
        super().__init__(title=es("📅 Создание мероприятия"))
        self.image_key = image_key
        self.num_games = num_games
        self.mandatory = mandatory
        self.event_title = discord.ui.TextInput(label="Название мероприятия", required=True, max_length=100)
        self.event_description = discord.ui.TextInput(label="Описание", style=discord.TextStyle.paragraph, required=True, max_length=1000)
        self.event_date = discord.ui.TextInput(label="Дата мероприятия (ДД.ММ.ГГГГ)", placeholder="01.12.2026", required=True, max_length=10)
        self.start_time = discord.ui.TextInput(label="Время начала (ЧЧ:ММ)", placeholder="19:30", required=True, max_length=5)
        self.end_time = discord.ui.TextInput(label="Время окончания (ЧЧ:ММ)", placeholder="22:00", required=True, max_length=5)
        self.add_item(self.event_title)
        self.add_item(self.event_description)
        self.add_item(self.event_date)
        self.add_item(self.start_time)
        self.add_item(self.end_time)

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            date_str = self.event_date.value.strip()
            start = MSK.localize(datetime.strptime(f"{date_str} {self.start_time.value.strip()}", "%d.%m.%Y %H:%M"))
            end = MSK.localize(datetime.strptime(f"{date_str} {self.end_time.value.strip()}", "%d.%m.%Y %H:%M"))
            if end <= start:
                end += timedelta(days=1)  # мероприятие переходит через полночь
            await create_event(self.event_title.value, self.event_description.value, start, end,
                                image_key=self.image_key, num_games=self.num_games, mandatory=self.mandatory)
            await interaction.followup.send(es("✅ Мероприятие создано!"), ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)

class EventEditModal(discord.ui.Modal):
    def __init__(self, event_id, current_title, current_description, current_date, current_start_time, current_end_time,
                 image_key='none', num_games=0, mandatory=True):
        super().__init__(title=es("✏️ Редактирование мероприятия"))
        self.event_id = event_id
        self.image_key = image_key
        self.num_games = num_games
        self.mandatory = mandatory
        self.event_title = discord.ui.TextInput(label="Название мероприятия", default=current_title, required=True, max_length=100)
        self.event_description = discord.ui.TextInput(label="Описание", style=discord.TextStyle.paragraph, default=current_description, required=True, max_length=1000)
        self.event_date = discord.ui.TextInput(label="Дата мероприятия (ДД.ММ.ГГГГ)", default=current_date, required=True, max_length=10)
        self.start_time = discord.ui.TextInput(label="Время начала (ЧЧ:ММ)", default=current_start_time, required=True, max_length=5)
        self.end_time = discord.ui.TextInput(label="Время окончания (ЧЧ:ММ)", default=current_end_time, required=True, max_length=5)
        self.add_item(self.event_title)
        self.add_item(self.event_description)
        self.add_item(self.event_date)
        self.add_item(self.start_time)
        self.add_item(self.end_time)

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            date_str = self.event_date.value.strip()
            start = MSK.localize(datetime.strptime(f"{date_str} {self.start_time.value.strip()}", "%d.%m.%Y %H:%M"))
            end = MSK.localize(datetime.strptime(f"{date_str} {self.end_time.value.strip()}", "%d.%m.%Y %H:%M"))
            if end <= start:
                end += timedelta(days=1)
            await update_event(self.event_id, self.event_title.value, self.event_description.value, start, end,
                                image_key=self.image_key, num_games=self.num_games, mandatory=self.mandatory)
            await interaction.followup.send(es("✅ Мероприятие обновлено!"), ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)


class EventSetupView(discord.ui.View):
    """Шаг настройки перед созданием мероприятия: картинка, количество игр,
    обязательность отметок — вынесены сюда из модалки (Select вместо текстовых
    полей), чтобы освободить место для разделения даты/времени на 3 поля,
    не превышая лимит Discord в 5 текстовых полей на одну модалку."""
    def __init__(self):
        super().__init__(timeout=180)
        self.image_key = 'none'
        self.num_games = 0
        self.mandatory = True

        image_options = [discord.SelectOption(label="Без картинки", value="none", emoji="🚫", default=True)]
        for key, data in EVENT_IMAGES.items():
            image_options.append(discord.SelectOption(label=data['title'], value=key, emoji="🖼️"))
        self.image_select = discord.ui.Select(placeholder="🖼️ Картинка (необязательно)...", options=image_options, row=0)
        self.image_select.callback = self._image_cb
        self.add_item(self.image_select)

        games_options = [discord.SelectOption(label=f"{n} {pluralize_games(n)}", value=str(n), default=(n == 0)) for n in range(MAX_GAMES + 1)]
        self.games_select = discord.ui.Select(placeholder="🎮 Количество игр...", options=games_options, row=1)
        self.games_select.callback = self._games_cb
        self.add_item(self.games_select)

        mandatory_options = [
            discord.SelectOption(label="Отметки обязательны", value="yes", emoji="✅", default=True),
            discord.SelectOption(label="Отметки необязательны", value="no", emoji="🚫"),
        ]
        self.mandatory_select = discord.ui.Select(placeholder="📌 Обязательность отметок...", options=mandatory_options, row=2)
        self.mandatory_select.callback = self._mandatory_cb
        self.add_item(self.mandatory_select)

        next_btn = discord.ui.Button(label=es("➡️ Далее"), style=discord.ButtonStyle.primary, row=3)
        next_btn.callback = self._next_cb
        self.add_item(next_btn)

    async def _image_cb(self, interaction):
        self.image_key = self.image_select.values[0]
        await interaction.response.defer()

    async def _games_cb(self, interaction):
        self.num_games = int(self.games_select.values[0])
        await interaction.response.defer()

    async def _mandatory_cb(self, interaction):
        self.mandatory = (self.mandatory_select.values[0] == "yes")
        await interaction.response.defer()

    async def _next_cb(self, interaction):
        self.stop()
        await interaction.response.send_modal(EventCreateModal(image_key=self.image_key, num_games=self.num_games, mandatory=self.mandatory))


class EventEditSetupView(discord.ui.View):
    """Аналог EventSetupView, но для редактирования — с предзаполнением
    текущих значений картинки/количества игр/обязательности."""
    def __init__(self, event_id):
        super().__init__(timeout=180)
        self.event_id = event_id
        events = load_json(EVENTS_FILE, {})
        event = events.get(event_id, {})
        self.image_key = "__keep__"
        self.num_games = event.get('num_games', 0)
        self.mandatory = event.get('mandatory', True)

        image_options = [discord.SelectOption(label="⏮️ Оставить текущую", value="__keep__", emoji="✅", default=True)]
        image_options.append(discord.SelectOption(label="Без картинки", value="none", emoji="🚫"))
        for key, data in EVENT_IMAGES.items():
            image_options.append(discord.SelectOption(label=data['title'], value=key, emoji="🖼️"))
        self.image_select = discord.ui.Select(placeholder="🖼️ Картинка (по умолчанию — текущая)...", options=image_options, row=0)
        self.image_select.callback = self._image_cb
        self.add_item(self.image_select)

        games_options = [discord.SelectOption(label=f"{n} {pluralize_games(n)}", value=str(n), default=(n == self.num_games)) for n in range(MAX_GAMES + 1)]
        self.games_select = discord.ui.Select(placeholder="🎮 Количество игр...", options=games_options, row=1)
        self.games_select.callback = self._games_cb
        self.add_item(self.games_select)

        mandatory_options = [
            discord.SelectOption(label="Отметки обязательны", value="yes", emoji="✅", default=self.mandatory),
            discord.SelectOption(label="Отметки необязательны", value="no", emoji="🚫", default=(not self.mandatory)),
        ]
        self.mandatory_select = discord.ui.Select(placeholder="📌 Обязательность отметок...", options=mandatory_options, row=2)
        self.mandatory_select.callback = self._mandatory_cb
        self.add_item(self.mandatory_select)

        next_btn = discord.ui.Button(label=es("➡️ Далее"), style=discord.ButtonStyle.primary, row=3)
        next_btn.callback = self._next_cb
        self.add_item(next_btn)

    async def _image_cb(self, interaction):
        self.image_key = self.image_select.values[0]
        await interaction.response.defer()

    async def _games_cb(self, interaction):
        self.num_games = int(self.games_select.values[0])
        await interaction.response.defer()

    async def _mandatory_cb(self, interaction):
        self.mandatory = (self.mandatory_select.values[0] == "yes")
        await interaction.response.defer()

    async def _next_cb(self, interaction):
        self.stop()
        await open_edit_modal(interaction, self.event_id, image_key=self.image_key, num_games=self.num_games, mandatory=self.mandatory)


class WeeklyEventSetupView(discord.ui.View):
    """Шаг 1: день недели, картинка, количество игр, обязательность.
    Шаг 2 — модалка только с текстовыми полями (название/описание/время)."""
    def __init__(self, weekly_id=None, defaults=None):
        super().__init__(timeout=180)
        self.weekly_id = weekly_id
        self.defaults = defaults or {}
        self.day_of_week = self.defaults.get('day_of_week', 'sat')
        self.image_key = self.defaults.get('image_key', 'none')
        self.num_games = self.defaults.get('num_games', 0)
        self.mandatory = self.defaults.get('mandatory', True)

        day_options = [
            discord.SelectOption(label=name, value=key, default=(key == self.day_of_week))
            for key, name in WEEKDAY_NAMES.items()
        ]
        self.day_select = discord.ui.Select(placeholder="📅 День недели...", options=day_options, row=0)
        self.day_select.callback = self._day_callback
        self.add_item(self.day_select)

        image_options = [discord.SelectOption(label="Без картинки", value="none", emoji="🚫", default=(self.image_key == 'none'))]
        for key, data in EVENT_IMAGES.items():
            image_options.append(discord.SelectOption(label=data['title'], value=key, emoji="🖼️", default=(key == self.image_key)))
        self.image_select = discord.ui.Select(placeholder="🖼️ Картинка...", options=image_options, row=1)
        self.image_select.callback = self._image_callback
        self.add_item(self.image_select)

        games_options = [discord.SelectOption(label=f"{n} {pluralize_games(n)}", value=str(n), default=(n == self.num_games)) for n in range(MAX_GAMES + 1)]
        self.games_select = discord.ui.Select(placeholder="🎮 Количество игр...", options=games_options, row=2)
        self.games_select.callback = self._games_callback
        self.add_item(self.games_select)

        mandatory_options = [
            discord.SelectOption(label="Отметки обязательны", value="yes", emoji="✅", default=self.mandatory),
            discord.SelectOption(label="Отметки необязательны", value="no", emoji="🚫", default=(not self.mandatory)),
        ]
        self.mandatory_select = discord.ui.Select(placeholder="📌 Обязательность отметок...", options=mandatory_options, row=3)
        self.mandatory_select.callback = self._mandatory_callback
        self.add_item(self.mandatory_select)

        next_btn = discord.ui.Button(label=es("➡️ Далее"), style=discord.ButtonStyle.primary, row=4)
        next_btn.callback = self._next_callback
        self.add_item(next_btn)

    async def _day_callback(self, interaction):
        self.day_of_week = self.day_select.values[0]
        await interaction.response.defer()

    async def _image_callback(self, interaction):
        self.image_key = self.image_select.values[0]
        await interaction.response.defer()

    async def _games_callback(self, interaction):
        self.num_games = int(self.games_select.values[0])
        await interaction.response.defer()

    async def _mandatory_callback(self, interaction):
        self.mandatory = (self.mandatory_select.values[0] == "yes")
        await interaction.response.defer()

    async def _next_callback(self, interaction):
        self.stop()
        await interaction.response.send_modal(
            WeeklyEventModal(weekly_id=self.weekly_id, day_of_week=self.day_of_week,
                              image_key=self.image_key, num_games=self.num_games,
                              mandatory=self.mandatory, defaults=self.defaults)
        )


class WeeklyEventModal(discord.ui.Modal):
    def __init__(self, weekly_id=None, day_of_week='sat', image_key='none', num_games=0, mandatory=True, defaults=None):
        super().__init__(title=es("🔁 Еженедельное мероприятие"))
        self.weekly_id = weekly_id
        self.day_of_week = day_of_week
        self.image_key = image_key
        self.num_games = num_games
        self.mandatory = mandatory
        defaults = defaults or {}
        self.name_input = discord.ui.TextInput(label="Название", default=clean_event_title(defaults.get('name', '')), required=True, max_length=100)
        self.description_input = discord.ui.TextInput(label="Описание", style=discord.TextStyle.paragraph, default=defaults.get('description', ''), required=True, max_length=1000)
        self.start_time_input = discord.ui.TextInput(label="Время начала (ЧЧ:ММ)", default=defaults.get('start_time', '16:30'), required=True, max_length=5)
        self.end_time_input = discord.ui.TextInput(label="Время окончания (ЧЧ:ММ)", default=defaults.get('end_time', '19:30'), required=True, max_length=5)
        self.add_item(self.name_input)
        self.add_item(self.description_input)
        self.add_item(self.start_time_input)
        self.add_item(self.end_time_input)

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            start_h, start_m = map(int, self.start_time_input.value.strip().split(':'))
            end_h, end_m = map(int, self.end_time_input.value.strip().split(':'))

            weekly_events = load_json(WEEKLY_EVENTS_FILE, {})
            entry = {
                'name': self.name_input.value,
                'description': self.description_input.value,
                'day_of_week': self.day_of_week,
                'start_time': f"{start_h:02d}:{start_m:02d}",
                'end_time': f"{end_h:02d}:{end_m:02d}",
                'image_key': self.image_key,
                'num_games': self.num_games,
                'mandatory': self.mandatory
            }
            weekly_id = self.weekly_id or str(uuid.uuid4())
            weekly_events[weekly_id] = entry
            save_json(WEEKLY_EVENTS_FILE, weekly_events)
            await interaction.followup.send(es(f"✅ Еженедельное мероприятие «{entry['name']}» сохранено!"), ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)


class WeeklyEventsManageSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        weekly_events = load_json(WEEKLY_EVENTS_FILE, {})
        options = [discord.SelectOption(label=entry.get('name', wid)[:100], value=wid)
                   for wid, entry in weekly_events.items()]
        if options:
            self.select = discord.ui.Select(placeholder="🔁 Выберите мероприятие для управления...", options=options[:MAX_SELECT_OPTIONS], row=0)
            self.select.callback = self._select_callback
            self.add_item(self.select)
        add_btn = discord.ui.Button(label=es("➕ Добавить новое"), style=discord.ButtonStyle.success, row=1)
        add_btn.callback = self._add_callback
        self.add_item(add_btn)

    async def _select_callback(self, interaction):
        weekly_id = self.select.values[0]
        weekly_events = load_json(WEEKLY_EVENTS_FILE, {})
        entry = weekly_events.get(weekly_id)
        if not entry:
            await interaction.response.send_message(es("❌ Не найдено!"), ephemeral=True)
            return
        text = (
            f"**{entry['name']}**\n\n{entry['description']}\n\n"
            f"День: {WEEKDAY_NAMES.get(entry['day_of_week'], entry['day_of_week'])}\n"
            f"Время: {entry['start_time']} - {entry['end_time']}\n"
            f"Матчей: {entry.get('num_games', 0)}\n"
            f"Обязательны ли отметки: {'Да' if entry.get('mandatory', True) else 'Нет'}"
        )
        await interaction.response.send_message(text, view=WeeklyEventManageActionsView(weekly_id), ephemeral=True)

    async def _add_callback(self, interaction):
        await interaction.response.send_message(
            es("📅 Настройте новое еженедельное мероприятие:"),
            view=WeeklyEventSetupView(), ephemeral=True
        )


class WeeklyEventManageActionsView(discord.ui.View):
    def __init__(self, weekly_id):
        super().__init__(timeout=180)
        self.weekly_id = weekly_id

    @discord.ui.button(label=es("✏️ Редактировать"), style=discord.ButtonStyle.primary)
    async def edit_btn(self, interaction, button):
        weekly_events = load_json(WEEKLY_EVENTS_FILE, {})
        entry = weekly_events.get(self.weekly_id)
        if not entry:
            await interaction.response.send_message(es("❌ Не найдено!"), ephemeral=True)
            return
        await interaction.response.send_message(
            es("📅 Измените день/картинку (или оставьте как есть) и нажмите Далее:"),
            view=WeeklyEventSetupView(weekly_id=self.weekly_id, defaults=entry), ephemeral=True
        )

    @discord.ui.button(label=es("🗑️ Удалить"), style=discord.ButtonStyle.danger)
    async def delete_btn(self, interaction, button):
        weekly_events = load_json(WEEKLY_EVENTS_FILE, {})
        if self.weekly_id in weekly_events:
            name = weekly_events[self.weekly_id].get('name', self.weekly_id)
            del weekly_events[self.weekly_id]
            save_json(WEEKLY_EVENTS_FILE, weekly_events)
            await interaction.response.send_message(es(f"✅ «{name}» удалено."), ephemeral=True)
        else:
            await interaction.response.send_message(es("❌ Не найдено!"), ephemeral=True)

class TestAnketaModal(discord.ui.Modal, title=es("🧪 Тестовая публикация анкеты")):
    profile_uid = discord.ui.TextInput(
        label="UID профиля в Firebase",
        placeholder="3Y1qZeszbihw5UM3y58bxz2ldP12",
        default="3Y1qZeszbihw5UM3y58bxz2ldP12",
        required=True, max_length=64
    )

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        uid = self.profile_uid.value.strip()
        if not fs_db:
            await interaction.followup.send(es("❌ Firebase недоступен (режим DATA_BACKEND='json')."), ephemeral=True)
            return
        try:
            loop = asyncio.get_event_loop()
            doc = await loop.run_in_executor(EXECUTOR, lambda: fs_db.collection('profiles').document(uid).get())
            if not doc.exists:
                await interaction.followup.send(es(f"❌ Профиль с UID `{uid}` не найден!"), ephemeral=True)
                return
            data = doc.to_dict() or {}
            channel = await client.fetch_channel(ANKETA_CHANNEL_ID)
            mention_block = get_anketa_leadership_mentions(channel.guild, data.get('gamesInterested', []))
            embed = await build_anketa_embed(uid, data)
            await channel.send(content=mention_block if mention_block else None, embed=embed)
            await interaction.followup.send(es(f"✅ Тестовая анкета опубликована в <#{ANKETA_CHANNEL_ID}>!"), ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)

class AdminMainMenuView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(label=es("📅 Создание единоразового мероприятия"), style=discord.ButtonStyle.primary, custom_id="admin_create_event", row=0)
    async def create_event_button(self, interaction, button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        view = EventSetupView()
        await interaction.response.send_message(es("📅 Настройте параметры мероприятия и нажмите Далее:"), view=view, ephemeral=True)
    
    @discord.ui.button(label=es("📋 Список единоразовых мероприятий"), style=discord.ButtonStyle.secondary, custom_id="admin_event_list", row=0)
    async def event_list_button(self, interaction, button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        await show_event_list(interaction)
        
    @discord.ui.button(label=es("🔁 Управление еженедельными мероприятиями"), style=discord.ButtonStyle.secondary, custom_id="admin_weekly_events", row=1)
    async def weekly_events_button(self, interaction, button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        ensure_weekly_events_file()
        await interaction.response.send_message(
            es("🔁 Управление еженедельными мероприятиями:"),
            view=WeeklyEventsManageSelectView(), ephemeral=True
        )
    
    @discord.ui.button(label=es("📝 Отправка сообщения"), style=discord.ButtonStyle.success, custom_id="admin_send_message", row=2)
    async def send_message_button(self, interaction, button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        await interaction.response.send_modal(SendMessageModal())
    
    @discord.ui.button(label=es("🗑️ Удаление сообщения"), style=discord.ButtonStyle.danger, custom_id="admin_delete_message", row=2)
    async def delete_message_button(self, interaction, button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        await interaction.response.send_modal(DeleteMessageModal())
    
    @discord.ui.button(label=es("🏖️ Отпуск для бойца"), style=discord.ButtonStyle.primary, custom_id="admin_vacation_for_player", row=3)
    async def vacation_for_player_button(self, interaction, button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        await interaction.response.send_modal(AdminVacationModal())
    
    @discord.ui.button(label=es("🏖️ Список отпусков"), style=discord.ButtonStyle.secondary, custom_id="admin_vacation_list", row=3)
    async def vacation_list_button(self, interaction, button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        await show_vacation_list(interaction)
    
    @discord.ui.button(label=es("🔍 Проверка таблицы"), style=discord.ButtonStyle.success, custom_id="admin_check_table", row=4)
    async def check_table_button(self, interaction, button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        if check_lock.locked():
            await interaction.response.send_message(es("⚠️ Проверка уже выполняется!"), ephemeral=True)
            return
        await interaction.response.send_message(es("🔍 Запускаю проверку таблицы..."), ephemeral=True)
        await check_spreadsheet()
    
    @discord.ui.button(label=es("🔍 Извлечение кода сообщения"), style=discord.ButtonStyle.secondary, custom_id="admin_extract_message", row=4)
    async def extract_message_button(self, interaction, button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        await interaction.response.send_modal(ExtractMessageModal())
    
    @discord.ui.button(label=es("🔄 Синхронизация оформления сообщений"), style=discord.ButtonStyle.primary, custom_id="admin_refresh_templates", row=4)
    async def refresh_templates_button(self, interaction, button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        await interaction.response.send_message(
            es("🔄 Начинаю обновление шаблонов всех сообщений...\n"
               "Это может занять несколько минут."),
            ephemeral=True
        )
        ev_updated, ev_errors, vac_updated, vac_errors, att_updated, att_errors, tn_fixed, tm_fixed, tl_fixed, anchors_fixed = await update_all_templates()
        await interaction.followup.send(
            es(f"📅 Мероприятий обновлено: **{ev_updated}** (ошибок: {ev_errors})\n"
               f"🏖️ Отпусков обновлено: **{vac_updated}** (ошибок: {vac_errors})\n"
               f"🏆 Отчётов о явке обновлено: **{att_updated}** (ошибок: {att_errors})\n"
               f"💬 Названий веток исправлено: **{tn_fixed}**, сообщений в ветках: **{tm_fixed}**, блокировок: **{tl_fixed}**\n"
               f"🛠️ Якорных сообщений обновлено: **{anchors_fixed}**"),
            ephemeral=True
        )
        

    @discord.ui.button(label=es("🧪 Тест: опубликовать анкету"), style=discord.ButtonStyle.secondary, custom_id="admin_test_anketa", row=4)
    async def test_anketa_button(self, interaction, button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        await interaction.response.send_modal(TestAnketaModal())

        
    @discord.ui.button(label=es("🔧 Принудительный перезапуск бота"), style=discord.ButtonStyle.danger, custom_id="admin_force_restart", row=4)
    async def force_restart_button(self, interaction, button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        await interaction.response.send_message(es("🔧 Перезапускаю бота... Новый процесс запустится через несколько секунд."), ephemeral=True)
        await asyncio.sleep(1)
        await force_restart_bot()

class VacationRequestView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    @discord.ui.button(label=es("🏖️ Оформить отпуск"), style=discord.ButtonStyle.primary, custom_id="vacation_request")
    async def vacation_button(self, interaction, button):
        await interaction.response.send_modal(VacationModal())


class VacationApprovalView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    def get_nickname_by_message(self, interaction):
        vacations = load_json(VACATIONS_FILE, {})
        for nickname, data in vacations.items():
            if data.get('message_id') == interaction.message.id:
                return nickname
        return None
    @discord.ui.button(label=es("✅ Утвердить отпуск"), style=discord.ButtonStyle.success, custom_id="vacation_approve")
    async def approve_button(self, interaction, button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        nickname = self.get_nickname_by_message(interaction)
        if not nickname:
            await interaction.response.send_message(es("❌ Отпуск не найден!"), ephemeral=True)
            return
        await approve_vacation(interaction, nickname)
    @discord.ui.button(label=es("❌ Отклонить отпуск"), style=discord.ButtonStyle.danger, custom_id="vacation_reject")
    async def reject_button(self, interaction, button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        nickname = self.get_nickname_by_message(interaction)
        if not nickname:
            await interaction.response.send_message(es("❌ Отпуск не найден!"), ephemeral=True)
            return
        await reject_vacation(interaction, nickname)


class VacationMessageView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    def get_nickname_by_message(self, interaction):
        vacations = load_json(VACATIONS_FILE, {})
        for nickname, data in vacations.items():
            if data.get('message_id') == interaction.message.id:
                return nickname
        return None
    @discord.ui.button(label=es("✅ Завершить мой отпуск досрочно"), style=discord.ButtonStyle.success, custom_id="vacation_end_early")
    async def end_early_button(self, interaction, button):
        nickname = self.get_nickname_by_message(interaction)
        if not nickname:
            await interaction.response.send_message(es("❌ Отпуск не найден!"), ephemeral=True)
            return
        if interaction.user.display_name != nickname and interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Только сам боец или командование!"), ephemeral=True)
            return
        await close_vacation(interaction, nickname, early=True, by_admin=False)
    @discord.ui.button(label=es("🔴 Закрыть отпуск для бойца"), style=discord.ButtonStyle.danger, custom_id="vacation_admin_close")
    async def admin_close_button(self, interaction, button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        nickname = self.get_nickname_by_message(interaction)
        if not nickname:
            await interaction.response.send_message(es("❌ Отпуск не найден!"), ephemeral=True)
            return
        await close_vacation(interaction, nickname, early=True, by_admin=True)


# ============== ДИНАМИЧЕСКИЕ КНОПКИ МЕРОПРИЯТИЙ ==============

def get_event_id_by_message_id(message_id):
    events = load_json(EVENTS_FILE, {})
    for event_id, event in events.items():
        if event.get('message_id') == message_id:
            return event_id
    return None


def event_created_late(event: dict) -> bool:
    """True, если мероприятие было создано менее чем за 24 часа до начала (п.12, п.13)."""
    created_at = event.get('created_at')
    if not created_at:
        return False
    return (event.get('start_time', 0) - created_at) < 24 * 3600

def desired_thread_name(event: dict) -> str:
    return f"💬 {build_display_title(event)}"[:100]


async def refresh_event_message(event_id):
    """Перерисовывает сообщение мероприятия (embed + актуальный набор кнопок)."""
    events = load_json(EVENTS_FILE, {})
    event = events.get(event_id)
    if not event:
        return
    try:
        channel = await client.fetch_channel(event['channel_id'])
        message = await channel.fetch_message(event['message_id'])
        embed = await build_event_embed(event_id)
        view = build_event_view(event, event_id)
        filename, path = get_image_info(event.get('image_key', 'none'))
        if filename and path:
            await message.edit(embed=embed, view=view, attachments=[discord.File(path, filename=filename)])
        else:
            await message.edit(embed=embed, view=view, attachments=[])
    except Exception as e:
        print(f"⚠️ Не удалось обновить сообщение мероприятия: {e}")


# --- Коллбэки кнопок (standalone-функции, чтобы работать в разных сочетаниях View) ---

async def on_accept_button(interaction: discord.Interaction):
    event_id = get_event_id_by_message_id(interaction.message.id)
    if not event_id:
        await interaction.response.send_message(es("❌ Мероприятие не найдено!"), ephemeral=True)
        return
    await handle_event_response(interaction, event_id, "accept")


async def on_decline_button(interaction: discord.Interaction):
    event_id = get_event_id_by_message_id(interaction.message.id)
    if not event_id:
        await interaction.response.send_message(es("❌ Мероприятие не найдено!"), ephemeral=True)
        return
    await handle_event_response(interaction, event_id, "decline")


async def on_edit_button(interaction: discord.Interaction):
    if interaction.user.id not in ADMIN_USER_IDS:
        await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
        return
    event_id = get_event_id_by_message_id(interaction.message.id)
    if not event_id:
        await interaction.response.send_message(es("❌ Мероприятие не найдено!"), ephemeral=True)
        return
    await interaction.response.send_message(
        es("✏️ Измените параметры (или оставьте как есть) и нажмите Далее:"), view=EventEditSetupView(event_id), ephemeral=True
    )


async def on_attendance_button(interaction: discord.Interaction):
    if interaction.user.id not in ADMIN_USER_IDS:
        await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
        return
    event_id = get_event_id_by_message_id(interaction.message.id)
    if not event_id:
        await interaction.response.send_message(es("❌ Мероприятие не найдено!"), ephemeral=True)
        return
    await start_attendance_wizard(interaction, event_id)


async def on_cancel_button(interaction: discord.Interaction):
    if interaction.user.id not in ADMIN_USER_IDS:
        await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
        return
    event_id = get_event_id_by_message_id(interaction.message.id)
    if not event_id:
        await interaction.response.send_message(es("❌ Мероприятие не найдено!"), ephemeral=True)
        return
    await cancel_event(interaction, event_id)


async def on_reactivate_button(interaction: discord.Interaction):
    if interaction.user.id not in ADMIN_USER_IDS:
        await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
        return
    event_id = get_event_id_by_message_id(interaction.message.id)
    if not event_id:
        await interaction.response.send_message(es("❌ Мероприятие не найдено!"), ephemeral=True)
        return
    await reactivate_event(interaction, event_id)


async def on_delete_button(interaction: discord.Interaction):
    if interaction.user.id not in ADMIN_USER_IDS:
        await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
        return
    event_id = get_event_id_by_message_id(interaction.message.id)
    if not event_id:
        await interaction.response.send_message(es("❌ Мероприятие не найдено!"), ephemeral=True)
        return
    await interaction.response.send_message(
        es("⚠️ Вы уверены, что хотите **полностью удалить** мероприятие? Это действие необратимо!"),
        view=ConfirmDeleteView(event_id), ephemeral=True
    )


async def on_mods_button(interaction: discord.Interaction):
    if interaction.user.id not in ADMIN_USER_IDS:
        await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
        return
    event_id = get_event_id_by_message_id(interaction.message.id)
    if not event_id:
        await interaction.response.send_message(es("❌ Мероприятие не найдено!"), ephemeral=True)
        return
    await interaction.response.send_modal(ModsAnnounceModal(event_id))


class ConfirmDeleteView(discord.ui.View):
    def __init__(self, event_id):
        super().__init__(timeout=60)
        self.event_id = event_id

    @discord.ui.button(label=es("🗑️ Да, удалить"), style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        await delete_event(interaction, self.event_id)

    @discord.ui.button(label=es("🚫 Отмена"), style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        await interaction.response.send_message(es("🚫 Удаление отменено."), ephemeral=True)


class ModsAnnounceModal(discord.ui.Modal, title=es("🧩 Объявление для скачивания модов")):
    server_name = discord.ui.TextInput(label="Название сервера (необязательно)", required=False, max_length=100)
    password = discord.ui.TextInput(label="Пароль сервера (необязательно)", required=False, max_length=100)

    def __init__(self, event_id):
        super().__init__()
        self.event_id = event_id

    async def on_submit(self, interaction):
        events = load_json(EVENTS_FILE, {})
        event = events.get(self.event_id)
        if not event:
            await interaction.response.send_message(es("❌ Мероприятие не найдено!"), ephemeral=True)
            return

        thread = await get_or_create_thread(event, self.event_id, event['title'])
        if not thread:
            await interaction.response.send_message(es("❌ Не удалось получить ветку мероприятия!"), ephemeral=True)
            return

        event_start = datetime.fromtimestamp(event['start_time'], MSK)
        start_ts = int(event_start.timestamp())
        current_time = datetime.now(MSK)

        if event_created_late(event):
            # Мероприятие создано менее чем за сутки — тегаем всех активных,
            # кто не в отпуске (роль 'Боец ArmA' целиком больше не пингуется).
            mention_block = await get_all_active_members_mentions(current_time)
        else:
            # По умолчанию — только тем, кто отметился "Приду"
            accepted = list(event.get('accepted', {}).keys())
            if accepted:
                mention_block = await build_mentions_for_nicknames(accepted)
            else:
                # Никто ещё не отметился — тегаем всех активных, кто не в отпуске
                mention_block = await get_all_active_members_mentions(current_time)

        server_name = self.server_name.value.strip()
        password = self.password.value.strip()
        text = render_mods_message(mention_block, event, server_name, password)

        msg = await thread.send(text)
        events_fresh = load_json(EVENTS_FILE, {})
        fresh_event = events_fresh.get(self.event_id)
        if fresh_event:
            record_thread_message(fresh_event, msg.id, 'mods', mention_block=mention_block,
                                   extra={'server_name': server_name, 'password': password})
            save_json(EVENTS_FILE, events_fresh)
        await interaction.response.send_message(es("✅ Объявление для скачивания модов отправлено!"), ephemeral=True)


# --- Фабрики кнопок ---

def make_accept_button():
    b = discord.ui.Button(label=es("✅ Приду"), style=discord.ButtonStyle.success, custom_id="event_accept", row=0)
    b.callback = on_accept_button
    return b

def make_decline_button():
    b = discord.ui.Button(label=es("❌ Не приду"), style=discord.ButtonStyle.danger, custom_id="event_decline", row=0)
    b.callback = on_decline_button
    return b

def make_edit_button():
    b = discord.ui.Button(label=es("✏️ Изменить"), style=discord.ButtonStyle.secondary, custom_id="event_edit", row=1)
    b.callback = on_edit_button
    return b

def make_attendance_button():
    b = discord.ui.Button(label=es("📝 Явка"), style=discord.ButtonStyle.success, custom_id="event_attendance", row=1)
    b.callback = on_attendance_button
    return b

def make_cancel_button():
    b = discord.ui.Button(label=es("🚫 Отменить"), style=discord.ButtonStyle.danger, custom_id="event_cancel", row=1)
    b.callback = on_cancel_button
    return b

def make_reactivate_button():
    b = discord.ui.Button(label=es("🔄 Активировать"), style=discord.ButtonStyle.success, custom_id="event_reactivate", row=1)
    b.callback = on_reactivate_button
    return b

def make_delete_button():
    b = discord.ui.Button(label=es("🗑️ Удалить"), style=discord.ButtonStyle.danger, custom_id="event_delete", row=1)
    b.callback = on_delete_button
    return b

def make_mods_button():
    b = discord.ui.Button(label=es("🧩 Моды"), style=discord.ButtonStyle.primary, custom_id="event_mods", row=1)
    b.callback = on_mods_button
    return b


def build_event_view(event: dict, event_id: str = None) -> discord.ui.View:
    """Строит нужный набор кнопок в зависимости от текущего статуса мероприятия.
    Кнопка '📝 Явка' ВСЕГДА доступна (даже если отчёт уже подан) — повторное
    заполнение позволяет исправить данные; пересчёт gameStats и очереди
    на командование корректно учитывает только изменения (см. finalize_attendance),
    а старое сообщение отчёта удаляется перед публикацией нового."""
    status = event.get('status', 'active')
    view = discord.ui.View(timeout=None)
    if status == 'active':
        view.add_item(make_accept_button())
        view.add_item(make_decline_button())
        view.add_item(make_edit_button())
        view.add_item(make_attendance_button())
        view.add_item(make_cancel_button())
        view.add_item(make_delete_button())
        view.add_item(make_mods_button())
    elif status == 'cancelled':
        view.add_item(make_edit_button())
        view.add_item(make_attendance_button())
        view.add_item(make_reactivate_button())
        view.add_item(make_delete_button())
    elif status == 'completed':
        view.add_item(make_edit_button())
        view.add_item(make_attendance_button())
        view.add_item(make_delete_button())
    return view


def register_persistent_event_views():
    """Регистрирует персистентные callback'и кнопок мероприятий раздельными View
    по каждому статусу — гарантированно без риска превысить лимит Discord
    (максимум 5 виджетов в строке, максимум 5 строк на одно View).
    custom_id совпадают между разными View (например, 'event_delete' есть
    во всех трёх) — это не проблема: discord.py просто резолвит клик
    по custom_id независимо от того, какое View изначально его зарегистрировало,
    а сама функция-колбэк у одинаковых custom_id всегда одна и та же."""
    for status in ('active', 'cancelled', 'completed'):
        client.add_view(build_event_view({'status': status}))

# ============== REALTIME-СЛЕЖЕНИЕ ЗА FIREBASE (анкеты / changeLog / уведомления) ==============

FIRESTORE_WATCH_HANDLES = []
MAIN_EVENT_LOOP = None
FIRESTORE_WATCHERS_STARTED = False


def _watcher_state_doc_ref():
    return fs_db.collection('botData').document('_watcherState')


def _get_watcher_state_sync():
    snap = _watcher_state_doc_ref().get()
    return (snap.to_dict() or {}) if snap.exists else {}


def _set_watcher_state_field_sync(key, epoch_ts):
    _watcher_state_doc_ref().set({key: epoch_ts}, merge=True)


async def get_watcher_last_ts(key):
    if not fs_db:
        return None
    loop = asyncio.get_event_loop()
    try:
        state = await loop.run_in_executor(EXECUTOR, _get_watcher_state_sync)
        return state.get(key)
    except Exception as e:
        print(f"⚠️ Не удалось прочитать состояние watcher'а '{key}': {e}")
        return None


async def set_watcher_last_ts(key, dt: datetime):
    if not fs_db:
        return
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(EXECUTOR, _set_watcher_state_field_sync, key, dt.timestamp())
    except Exception as e:
        print(f"⚠️ Не удалось сохранить состояние watcher'а '{key}': {e}")


def _extract_timestamp(value):
    """Приводит значение поля createdAt (Firestore Timestamp) к datetime с tzinfo."""
    if isinstance(value, datetime):
        return value if value.tzinfo else pytz.UTC.localize(value)
    return datetime.now(pytz.UTC)


def _query_invited_by_sync(uid):
    docs = fs_db.collection('profiles').where(filter=FieldFilter('referredByUid', '==', uid)).stream()
    result = []
    for d in docs:
        dd = d.to_dict() or {}
        cs = (dd.get('callsign') or '').strip()
        if cs:
            result.append(cs)
    return result


async def get_invited_by_uid(uid):
    if not fs_db:
        return []
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(EXECUTOR, _query_invited_by_sync, uid)
    except Exception as e:
        print(f"⚠️ Ошибка запроса 'кого пригласил' для {uid}: {e}")
        return []


# --- Форматирование сообщений ---

async def build_anketa_embed(uid, data) -> discord.Embed:
    """Формирует красиво оформленный embed с анкетой кандидата."""
    callsign = data.get('callsign', '?')

    embed = discord.Embed(
        title=es(f"📋 Новая анкета: {callsign}"),
        color=discord.Color.gold()
    )

    steam_url = data.get('steamProfileUrl') or ''
    contact_lines = [
        f"**Эл. почта:** {data.get('email') or '—'}",
        f"**Discord ID:** {data.get('discordId') or '—'}",
        f"**Steam ID:** {data.get('steamId') or '—'}",
        f"**Steam-профиль:** [ссылка]({steam_url})" if steam_url else "**Steam-профиль:** —",
        f"**Arma ID:** {data.get('armaId') or '—'}",
    ]
    embed.add_field(name=es("👤 Личные данные"),
                     value=(f"**Имя:** {data.get('fullName') or '—'}\n"
                            f"**Возраст:** {data.get('age', '—')}\n"
                            f"**Дата рождения:** {data.get('birthDate') or '—'}\n"
                            f"**Часовой пояс:** {data.get('timezone') or '—'}"),
                     inline=False)
    embed.add_field(name=es("📞 Контакты"), value="\n".join(contact_lines), inline=False)

    extra = data.get('extraContacts', {}) or {}
    extra_lines = []
    if extra.get('phone'):
        extra_lines.append(f"**Телефон:** {extra['phone']}")
    if data.get('telegramUrl'):
        extra_lines.append(f"**Telegram:** [ссылка]({data['telegramUrl']})")
    if data.get('vkUrl'):
        extra_lines.append(f"**ВКонтакте:** [ссылка]({data['vkUrl']})")
    if extra.get('other'):
        extra_lines.append(f"**Другое:** {extra['other']}")
    if extra_lines:
        embed.add_field(name=es("📇 Доп. контакты"), value="\n".join(extra_lines), inline=False)

    referrer = data.get('referrerCallsign') or data.get('referredByText') or ''
    invited = await get_invited_by_uid(uid)
    how_found = data.get('howFound') or ''
    if not how_found:
        how_found = "Приглашён бойцом (см. ниже)" if referrer else "—"
    embed.add_field(name=es("🔗 Приглашения"),
                     value=(f"**Кем приглашён:** {referrer if referrer else '—'}\n"
                            f"**Кого пригласил:** {', '.join(invited) if invited else '—'}\n"
                            f"**Откуда узнал:** {how_found}"),
                     inline=False)

    embed.add_field(name=es("🕒 Доступность"), value=data.get('availability') or '—', inline=False)
    embed.add_field(name=es("💬 Почему хочет вступить"), value=data.get('whyJoin') or '—', inline=False)

    games = data.get('gamesInterested', []) or []
    exp_by_game = data.get('experienceByGame', {}) or {}
    hours_by_game = data.get('hoursByGame', {}) or {}
    if games:
        exp_lines = []
        for game in games:
            hours = hours_by_game.get(game)
            hours_str = f"{hours} ч." if hours is not None else "? ч."
            exp_text = exp_by_game.get(game, '')
            exp_lines.append(f"**{game}:** {hours_str}" + (f" — {exp_text}" if exp_text else ""))
        embed.add_field(name=es("🎮 Игровой опыт"), value="\n".join(exp_lines), inline=False)

    embed.set_footer(text=f"UID: {uid}")
    return embed


async def build_changelog_message(uid, data):
    callsign = data.get('callsign', '?')
    changed_by = data.get('changedBy', '')
    changes = data.get('changes', []) or []
    lines = []
    for ch in changes:
        field = ch.get('field', '?')
        old_val = ch.get('oldValue', '')
        new_val = ch.get('newValue', '')
        old_display = old_val if old_val not in (None, '') else '—'
        new_display = new_val if new_val not in (None, '') else '—'
        lines.append(f"{field}: {old_display} → {new_display}")
    if not lines:
        lines.append("Изменения не содержат деталей.")
    body = "\n".join(f"> {line}" for line in lines)
    who = "администрацией" if changed_by == 'admin' else ("самим бойцом" if changed_by else "неизвестно кем")
    header = f"**📝 Боец {callsign} изменил данные в своём профиле ({who}):**"
    return header + "\n\n" + body


async def build_notification_message(uid, data):
    message_text = data.get('message') or '—'
    callsign = await get_uid_callsign(uid)
    nickname = f"{CLAN_TAG}{callsign}" if callsign else (uid or '?')
    header = f"**🔔 Новое уведомление для бойца {nickname}:**"
    return header + "\n\n" + f"> {message_text}"


# --- Обработчики новых документов ---

async def handle_new_profile_watch(doc_id, data):
    try:
        channel = await client.fetch_channel(ANKETA_CHANNEL_ID)
        mention_block = get_anketa_leadership_mentions(channel.guild, data.get('gamesInterested', []))
        embed = await build_anketa_embed(doc_id, data)
        await channel.send(content=mention_block if mention_block else None, embed=embed)
    except Exception as e:
        print(f"❌ Ошибка публикации новой анкеты ({doc_id}): {e}")
    finally:
        await set_watcher_last_ts('profiles', _extract_timestamp(data.get('createdAt')))


async def handle_new_changelog_watch(doc_id, data):
    try:
        channel = await client.fetch_channel(ANKETA_CHANNEL_ID)
        text = await build_changelog_message(doc_id, data)
        await send_chunked(channel, text)
    except Exception as e:
        print(f"❌ Ошибка публикации записи changeLog ({doc_id}): {e}")
    finally:
        await set_watcher_last_ts('changeLog', _extract_timestamp(data.get('createdAt')))

async def handle_new_notification_watch(doc_id, data):
    try:
        thread_id = await get_notifications_thread_id()
        if thread_id:
            thread = await client.fetch_channel(thread_id)
            text = await build_notification_message(data.get('uid', ''), data)
            await send_chunked(thread, text)
    except Exception as e:
        print(f"❌ Ошибка публикации уведомления ({doc_id}): {e}")
    finally:
        await set_watcher_last_ts('notifications', _extract_timestamp(data.get('createdAt')))

def _make_on_added_callback(handler_coro):
    """Обёртка над Firestore watch-колбэком (выполняется в отдельном grpc-потоке).
    Передаёт обработку в основной event loop бота через run_coroutine_threadsafe."""
    def _callback(col_snapshot, changes, read_time):
        for change in changes:
            if change.type.name != 'ADDED':
                continue
            doc = change.document
            data = doc.to_dict() or {}
            if MAIN_EVENT_LOOP:
                try:
                    asyncio.run_coroutine_threadsafe(handler_coro(doc.id, data), MAIN_EVENT_LOOP)
                except Exception as e:
                    print(f"❌ Ошибка планирования обработки документа {doc.id}: {e}")
    return _callback


async def setup_firestore_watchers():
    """Запускает realtime-слежение (push, без задержки) за новыми анкетами,
    записями changeLog и уведомлениями. При первом включении фичи не постит
    историю — точка отсчёта ставится на текущий момент."""
    global FIRESTORE_WATCHERS_STARTED, MAIN_EVENT_LOOP
    if FIRESTORE_WATCHERS_STARTED:
        return
    FIRESTORE_WATCHERS_STARTED = True

    if not fs_db:
        print("⚠️ Firebase не инициализирован — realtime-уведомления не будут работать")
        return

    MAIN_EVENT_LOOP = asyncio.get_running_loop()

    watch_configs = [
        ('profiles', handle_new_profile_watch),
        ('changeLog', handle_new_changelog_watch),
        ('notifications', handle_new_notification_watch),
    ]

    for collection_name, handler in watch_configs:
        try:
            last_ts_epoch = await get_watcher_last_ts(collection_name)
            if last_ts_epoch is None:
                threshold = datetime.now(pytz.UTC)
                await set_watcher_last_ts(collection_name, threshold)
            else:
                threshold = datetime.fromtimestamp(last_ts_epoch, tz=pytz.UTC)

            query = fs_db.collection(collection_name).where(filter=FieldFilter('createdAt', '>', threshold))
            watch = query.on_snapshot(_make_on_added_callback(handler))
            FIRESTORE_WATCH_HANDLES.append(watch)
            print(f"✅ Запущено realtime-слежение за коллекцией '{collection_name}'")
        except Exception as e:
            print(f"❌ Не удалось запустить слежение за '{collection_name}': {e}")

# ============== УЧЁТ ОТЫГРЫШЕЙ (GAMESTATS) ==============

GAMESTATS_GAME_NAME = "Arma Reforger"

CALLSIGN_UID_CACHE = {}
CALLSIGN_UID_CACHE_TIME = {}
CALLSIGN_UID_CACHE_TTL = 3600


def _normalize_callsign_for_lookup(nickname_with_tag: str) -> str:
    name = nickname_with_tag
    if name.startswith(CLAN_TAG):
        name = name[len(CLAN_TAG):]
    return name.strip().lower()


def _firebase_lookup_uid_by_callsign_sync(callsign_lower):
    doc = fs_db.collection('callsigns').document(callsign_lower).get()
    if doc.exists:
        return (doc.to_dict() or {}).get('uid')
    return None


async def get_uid_by_nickname(nickname: str):
    key = _normalize_callsign_for_lookup(nickname)
    now = datetime.now().timestamp()
    cached_time = CALLSIGN_UID_CACHE_TIME.get(key, 0)
    if key in CALLSIGN_UID_CACHE and (now - cached_time) < CALLSIGN_UID_CACHE_TTL:
        return CALLSIGN_UID_CACHE[key]
    if not fs_db:
        return CALLSIGN_UID_CACHE.get(key)
    try:
        loop = asyncio.get_event_loop()
        uid = await loop.run_in_executor(EXECUTOR, _firebase_lookup_uid_by_callsign_sync, key)
        CALLSIGN_UID_CACHE[key] = uid
        CALLSIGN_UID_CACHE_TIME[key] = now
        return uid
    except Exception as e:
        print(f"⚠️ Не удалось найти uid для позывного '{nickname}': {e}")
        return CALLSIGN_UID_CACHE.get(key)


def _apply_gamestats_increment_sync(uid, ko_delta, ks_delta, soldier_delta):
    doc_ref = fs_db.collection('profiles').document(uid)
    updates = {}
    if ko_delta:
        updates[f'gameStats.{GAMESTATS_GAME_NAME}.koCount'] = firestore.Increment(ko_delta)
    if ks_delta:
        updates[f'gameStats.{GAMESTATS_GAME_NAME}.ksCount'] = firestore.Increment(ks_delta)
    if soldier_delta:
        updates[f'gameStats.{GAMESTATS_GAME_NAME}.playedAsSoldierCount'] = firestore.Increment(soldier_delta)
    if not updates:
        return
    try:
        doc_ref.update(updates)
    except Exception:
        # Структуры gameStats/{game} у профиля ещё нет — создаём с нуля
        doc_ref.set({
            'gameStats': {
                GAMESTATS_GAME_NAME: {
                    'koCount': max(ko_delta, 0),
                    'ksCount': max(ks_delta, 0),
                    'playedAsSoldierCount': max(soldier_delta, 0),
                }
            }
        }, merge=True)


def _create_notification_sync(uid, message):
    fs_db.collection('notifications').add({
        'uid': uid,
        'message': message,
        'read': False,
        'createdAt': firestore.SERVER_TIMESTAMP,
    })


async def create_gamestats_notification(uid, message):
    if not fs_db or not message:
        return
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(EXECUTOR, _create_notification_sync, uid, message)
    except Exception as e:
        print(f"⚠️ Не удалось создать уведомление для {uid}: {e}")


def build_gamestats_notification_message(ko_delta, ks_delta, soldier_delta):
    parts = []
    if soldier_delta > 0:
        parts.append(("бойца", soldier_delta))
    if ko_delta > 0:
        parts.append(("командира отделения", ko_delta))
    if ks_delta > 0:
        parts.append(("командира стороны", ks_delta))
    if not parts:
        return None
    if len(parts) == 1:
        label, count = parts[0]
        return f"{GAMESTATS_GAME_NAME}: зачтён отыгрыш за {label} (+{count})."
    joined = ", ".join(f"за {label} (+{count})" for label, count in parts)
    return f"{GAMESTATS_GAME_NAME}: зачтены отыгрыши: {joined}."


def _extract_game_triples_from_wizard(wizard):
    """[(players, squad_commanders_list, side_commander), ...] — один элемент на игру."""
    if wizard.num_games == 0:
        return [(wizard.data.get('overall', []), wizard.commanders.get('overall') or [], wizard.side_commanders.get('overall'))]
    return [
        (wizard.data.get(i, []), wizard.commanders.get(i) or [], wizard.side_commanders.get(i))
        for i in range(wizard.num_games)
    ]


def _normalize_commanders_field(record_part, singular_key, plural_key):
    """Обратная совместимость: старые записи хранили ОДНОГО командира
    в поле singular_key (строка), новые — список в plural_key."""
    if plural_key in record_part:
        val = record_part.get(plural_key) or []
        return val if isinstance(val, list) else ([val] if val else [])
    val = record_part.get(singular_key)
    return [val] if val else []


def _extract_game_triples_from_record(record):
    """То же самое, но из уже сохранённой в Firebase записи явки
    (нужно для вычисления 'старых' отыгрышей и 'старых' командиров
    при повторной подаче явки — чтобы не задваивать инкременты и сдвиги очереди)."""
    if not record:
        return []
    if 'overall_players' in record:
        commanders = _normalize_commanders_field(record, 'overall_commander', 'overall_commanders')
        return [(record.get('overall_players', []), commanders, record.get('overall_side_commander'))]
    games = record.get('games', {}) or {}
    triples = []
    for key in sorted(games.keys(), key=lambda k: int(k) if k.isdigit() else 0):
        g = games[key]
        commanders = _normalize_commanders_field(g, 'commander', 'commanders')
        triples.append((g.get('players', []), commanders, g.get('side_commander')))
    return triples


def _tally_from_triples(triples):
    """Считает отыгрыши по ролям. Поддерживает НЕСКОЛЬКО командиров отделения
    на одну игру — каждый из них корректно получает +1 к koCount."""
    per_player = {}

    def bump(nickname, field):
        per_player.setdefault(nickname, {'ko': 0, 'ks': 0, 'soldier': 0})[field] += 1

    for players, squad_commanders, side_commander in triples:
        squad_commanders = squad_commanders or []
        for player in (players or []):
            is_ko = player in squad_commanders
            is_ks = (player == side_commander)
            if is_ko:
                bump(player, 'ko')
            if is_ks:
                bump(player, 'ks')
            if not is_ko and not is_ks:
                bump(player, 'soldier')
    return per_player

def _move_uid_to_queue_back_sync(uid):
    """Атомарно (в транзакции Firestore) перемещает uid в конец очереди
    /queue/state.current, если он там присутствует. Если uid не найден —
    ничего не делает: самовольно добавлять кого-либо в очередь — не задача
    бота, это прерогатива администрации/сайта."""
    doc_ref = fs_db.collection('queue').document('state')
    transaction = fs_db.transaction()

    @firestore.transactional
    def _txn(transaction, doc_ref, uid):
        snapshot = doc_ref.get(transaction=transaction)
        data = snapshot.to_dict() if snapshot.exists else {}
        current = list(data.get('current', []) or [])
        idx = None
        for i, entry in enumerate(current):
            if isinstance(entry, dict) and entry.get('uid') == uid:
                idx = i
                break
        if idx is None:
            return False
        entry = current.pop(idx)
        current.append(entry)
        transaction.set(doc_ref, {'current': current}, merge=True)
        return True

    return _txn(transaction, doc_ref, uid)


async def move_squad_commander_to_queue_back(nickname: str):
    uid = await get_uid_by_nickname(nickname)
    if not uid:
        print(f"⚠️ Не удалось найти uid для '{nickname}' — сдвиг очереди пропущен")
        return
    loop = asyncio.get_event_loop()
    try:
        moved = await loop.run_in_executor(EXECUTOR, _move_uid_to_queue_back_sync, uid)
        if moved:
            print(f"🔁 '{nickname}' сдвинут в конец очереди на командование отделением")
    except Exception as e:
        print(f"❌ Ошибка сдвига очереди для '{nickname}': {e}")
    # Инвалидируем локальный кэш очереди, чтобы embed сразу показал актуальные данные
    QUEUE_CACHE['current'] = None
    QUEUE_CACHE['time'] = 0


async def apply_squad_commander_queue_promotion(wizard, old_record=None):
    """Сдвигает в конец очереди на командование отделением ВСЕХ НОВЫХ
    (не назначавшихся ранее в этой же явке — важно при повторном редактировании)
    командиров отделения, по одному, СТРОГО в порядке игр: сначала командир(ы)
    первой игры, затем второй, и т.д. Если на одной игре несколько командиров —
    порядок между ними не критичен (двигаются последовательно друг за другом).
    Возвращает True, если была хотя бы одна фактическая перестановка."""
    new_triples = _extract_game_triples_from_wizard(wizard)
    old_triples = _extract_game_triples_from_record(old_record) if old_record else []

    ordered_new_commanders = []
    for game_idx, (_, new_commanders, _) in enumerate(new_triples):
        old_commanders = old_triples[game_idx][1] if game_idx < len(old_triples) else []
        for nickname in (new_commanders or []):
            if nickname not in old_commanders:
                ordered_new_commanders.append(nickname)

    for nickname in ordered_new_commanders:
        await move_squad_commander_to_queue_back(nickname)

    return len(ordered_new_commanders) > 0


async def refresh_all_active_event_embeds():
    """Обновляет embed всех активных (ещё не начавшихся/идущих) мероприятий —
    вызывается после сдвига очереди, чтобы поле 'Ожидаемый командир отделения'
    сразу отражало актуальное состояние во всех будущих мероприятиях,
    а не только в том, по которому заполнялась явка."""
    events = load_json(EVENTS_FILE, {})
    for event_id, event in events.items():
        if event.get('status', 'active') != 'active':
            continue
        await refresh_event_message(event_id)
        await asyncio.sleep(1)


async def apply_attendance_to_gamestats(wizard, old_record=None):
    """Считает НЕТТО-изменение отыгрышей (новая явка минус старая, если это
    повторная подача) и инкрементит gameStats в Firebase + создаёт уведомление
    игроку при положительном приросте (уведомление автоматически попадёт
    в Discord-канал через realtime-watcher из п.7)."""
    new_tally = _tally_from_triples(_extract_game_triples_from_wizard(wizard))
    old_tally = _tally_from_triples(_extract_game_triples_from_record(old_record)) if old_record else {}

    for nickname in set(new_tally) | set(old_tally):
        new_c = new_tally.get(nickname, {'ko': 0, 'ks': 0, 'soldier': 0})
        old_c = old_tally.get(nickname, {'ko': 0, 'ks': 0, 'soldier': 0})
        ko_delta = new_c['ko'] - old_c['ko']
        ks_delta = new_c['ks'] - old_c['ks']
        soldier_delta = new_c['soldier'] - old_c['soldier']

        if ko_delta == 0 and ks_delta == 0 and soldier_delta == 0:
            continue

        uid = await get_uid_by_nickname(nickname)
        if not uid:
            print(f"⚠️ Не удалось найти uid для '{nickname}' — отыгрыши не зачтены в Firebase")
            continue

        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(EXECUTOR, _apply_gamestats_increment_sync, uid, ko_delta, ks_delta, soldier_delta)
        except Exception as e:
            print(f"❌ Ошибка обновления gameStats для {nickname} ({uid}): {e}")
            continue

        message = build_gamestats_notification_message(max(ko_delta, 0), max(ks_delta, 0), max(soldier_delta, 0))
        if message:
            await create_gamestats_notification(uid, message)

def _is_active_entry(entry, now_ms):
    return entry.get('expiresAtMs', 0) > now_ms


def _build_warning_reason(number: int) -> str:
    if number <= 1:
        return ("Неактивность. Замечание #1. Боец не отметился на мероприятии с обязательной "
                "записью и не находился в отпуске. Нарушение п. 9 Устава")
    return (f"Неактивность. Замечание #{number}. Боец повторно не отметился на мероприятии с обязательной "
            "записью для всех бойцов и не находился в отпуске. Нарушение п. 9 Устава")


def _build_reprimand_reason(number: int) -> str:
    return (f"Неактивность. Выговор #{number}. Боец игнорирует неоднократные замечания о необходимости "
            "отметок на мероприятия с обязательной записью и не находится в отпуске. Нарушение п. 9 Устава")


def _build_disc_entry(entry_type: str, reason: str, duration: timedelta, now_ms: int) -> dict:
    return {
        'id': f"{now_ms}-{uuid.uuid4().hex[:6]}",
        'type': entry_type,
        'reason': reason,
        'scope': GAMESTATS_GAME_NAME,
        'issuedAtMs': now_ms,
        'expiresAtMs': now_ms + int(duration.total_seconds() * 1000),
        'source': 'auto_inactivity',
    }


def _apply_disciplinary_action_sync(uid):
    """Транзакционно выносит замечание ИЛИ выговор (в зависимости от того,
    что ещё не исчерпано из максимума 3), учитывая ВСЕ действующие записи
    (не только автоматические) при подсчёте лимита. Нумерация #1/#2/#3 в
    тексте — только по автоматическим записям причины 'Неактивность'.
    При достижении 3 действующих выговоров (любого происхождения) —
    переводит в 'Отставка'/'Дезертир' и в profiles, и в rosterPublic."""
    profile_ref = fs_db.collection('profiles').document(uid)
    roster_ref = fs_db.collection('rosterPublic').document(uid)
    transaction = fs_db.transaction()

    @firestore.transactional
    def _txn(transaction):
        snap = profile_ref.get(transaction=transaction)
        if not snap.exists:
            return {'action': None}
        data = snap.to_dict() or {}
        game_da = data.get('gameDisciplinaryActions', {}) or {}
        actions = list(game_da.get(GAMESTATS_GAME_NAME, []) or [])
        now_ms = int(datetime.now(MSK).timestamp() * 1000)

        def active(a, t):
            return _is_active_entry(a, now_ms) and a.get('type') == t

        active_warnings = [a for a in actions if active(a, 'Замечание')]
        active_reprimands = [a for a in actions if active(a, 'Выговор')]

        result = {'action': None, 'expelled': False}

        if len(active_warnings) < MAX_ACTIVE_WARNINGS:
            number = len([a for a in active_warnings if a.get('source') == 'auto_inactivity']) + 1
            reason = _build_warning_reason(number)
            actions.append(_build_disc_entry('Замечание', reason, WARNING_DURATION, now_ms))
            result['action'] = 'warning'
            result['reason'] = reason
        elif len(active_reprimands) < MAX_ACTIVE_REPRIMANDS:
            number = len([a for a in active_reprimands if a.get('source') == 'auto_inactivity']) + 1
            reason = _build_reprimand_reason(number)
            actions.append(_build_disc_entry('Выговор', reason, REPRIMAND_DURATION, now_ms))
            result['action'] = 'reprimand'
            result['reason'] = reason
        # иначе: уже максимум и того, и другого — ничего не добавляем,
        # но ниже всё равно проверим порог исключения (например, если 3
        # выговора набрались ранее вручную, без участия бота).

        current_composition = ((data.get('gameRoles') or {}).get(GAMESTATS_GAME_NAME) or {}).get('composition', '')
        final_active_reprimands = [a for a in actions if active(a, 'Выговор')]
        should_expel = len(final_active_reprimands) >= MAX_ACTIVE_REPRIMANDS and current_composition != 'Отставка'

        updates = {f'gameDisciplinaryActions.{GAMESTATS_GAME_NAME}': actions}
        roster_updates = {f'gameDisciplinaryActions.{GAMESTATS_GAME_NAME}': actions}

        if should_expel:
            updates[f'gameRoles.{GAMESTATS_GAME_NAME}.composition'] = 'Отставка'
            updates[f'gameRoles.{GAMESTATS_GAME_NAME}.position'] = 'Дезертир'
            roster_updates[f'gameRoles.{GAMESTATS_GAME_NAME}.composition'] = 'Отставка'
            roster_updates[f'gameRoles.{GAMESTATS_GAME_NAME}.position'] = 'Дезертир'
            result['expelled'] = True

        transaction.update(profile_ref, updates)
        transaction.update(roster_ref, roster_updates)
        return result

    return _txn(transaction)


async def apply_inactivity_discipline(uid):
    if not fs_db:
        return None
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(EXECUTOR, _apply_disciplinary_action_sync, uid)
    except Exception as e:
        print(f"❌ Ошибка применения дисциплинарного взыскания (uid={uid}): {e}")
        return None


async def process_inactivity_discipline_for_event(event_id):
    """Выносит замечания/выговоры за неактивность всем, кто не отметился на
    ОБЯЗАТЕЛЬНОМ мероприятии (и не был в отпуске), для мероприятий начиная
    с DISCIPLINE_CUTOFF. Выполняется РОВНО ОДИН РАЗ на мероприятие."""
    events = load_json(EVENTS_FILE, {})
    event = events.get(event_id)
    if not event or event.get('discipline_processed'):
        return

    if not event.get('mandatory', True):
        event['discipline_processed'] = True
        save_json(EVENTS_FILE, events)
        return

    event_start_dt = datetime.fromtimestamp(event['start_time'], MSK)
    if event_start_dt < DISCIPLINE_CUTOFF:
        event['discipline_processed'] = True
        save_json(EVENTS_FILE, events)
        return

    current_time = datetime.now(MSK)
    active_members = await get_active_members(current_time, registered_before=event_start_dt)
    accepted = set(event.get('accepted', {}).keys())
    declined = set(event.get('declined', {}).keys())
    unmarked = [m for m in active_members if m not in accepted and m not in declined]

    event['discipline_processed'] = True
    save_json(EVENTS_FILE, events)

    if not unmarked:
        return

    thread = await get_or_create_thread(event, event_id, event['title'])
    any_expelled = False

    for nickname in unmarked:
        uid = await get_uid_by_nickname(nickname)
        if not uid:
            continue
        result = await apply_inactivity_discipline(uid)
        if not result or not result.get('action'):
            continue

        member = await find_member_by_nickname(nickname)
        mention = member.mention if member else f"**{nickname}**"
        action_word = "замечание" if result['action'] == 'warning' else "выговор"

        if thread:
            text = (
                f"{mention}\n\n" +
                es(f"⚠️ Боец, тебе вынесено {action_word} за неактивность:\n\n") +
                f"> {result['reason']}\n\n" +
                "Пожалуйста, не забывай отмечаться на мероприятиях с обязательной записью, если ты не находишься "
                "в отпуске. Подробности — в твоём личном деле на сайте клана."
            )
            try:
                await thread.send(text)
            except Exception as e:
                print(f"⚠️ Не удалось отправить уведомление о взыскании для {nickname}: {e}")

        if result.get('expelled'):
            any_expelled = True
            if member:
                guild = member.guild
                roles_to_check = [guild.get_role(ROLE_IDS[key]) for key in EXPULSION_ROLE_KEYS]
                present_roles = [r for r in roles_to_check if r and r in member.roles]
                if present_roles:
                    try:
                        await member.remove_roles(*present_roles, reason="3 действующих выговора за неактивность")
                    except Exception as e:
                        print(f"⚠️ Не удалось снять роли у {nickname}: {e}")
            if thread:
                try:
                    await thread.send(
                        f"{mention}\n\n" +
                        es("🚫 Боец достиг 3 действующих выговоров за неактивность и был исключен из клана.")
                    )
                except Exception:
                    pass

        await asyncio.sleep(1)

    if any_expelled:
        invalidate_clan_members_cache()
        await refresh_all_active_event_embeds()


def _cleanup_expired_disciplinary_actions_sync():
    """Убирает ИСТЁКШИЕ записи (любого происхождения) из gameDisciplinaryActions
    в profiles и rosterPublic. НИКОГДА не трогает composition/position/роли —
    исключение необратимо без ручного вмешательства администрации."""
    now_ms = int(datetime.now(MSK).timestamp() * 1000)
    docs = fs_db.collection('profiles').stream()
    cleaned = 0
    for doc in docs:
        data = doc.to_dict() or {}
        game_da = (data.get('gameDisciplinaryActions') or {}).get(GAMESTATS_GAME_NAME, [])
        if not game_da:
            continue
        filtered = [a for a in game_da if a.get('expiresAtMs', 0) > now_ms]
        if len(filtered) != len(game_da):
            uid = doc.id
            fs_db.collection('profiles').document(uid).update({f'gameDisciplinaryActions.{GAMESTATS_GAME_NAME}': filtered})
            fs_db.collection('rosterPublic').document(uid).update({f'gameDisciplinaryActions.{GAMESTATS_GAME_NAME}': filtered})
            cleaned += 1
    return cleaned


async def cleanup_expired_disciplinary_actions():
    if not fs_db:
        return
    loop = asyncio.get_event_loop()
    try:
        cleaned = await loop.run_in_executor(EXECUTOR, _cleanup_expired_disciplinary_actions_sync)
        if cleaned:
            print(f"🧹 Удалено просроченных замечаний/выговоров у {cleaned} бойцов")
    except Exception as e:
        print(f"❌ Ошибка очистки просроченных взысканий: {e}")


# ============== МАСТЕР УЧЁТА ЯВКИ (с командирами отделений) ==============

def build_attendance_report_text(record: dict) -> str:
    """Единая точка генерации текста отчёта о явке — используется и при
    первом создании отчёта, и при повторной синхронизации оформления."""
    title = record.get('title', '')
    report_text = es(f"🏆 **Отчёт о явке: {title}**\n\n")
    report_text += es(f"📋 Составил: **{record.get('reported_by', '?')}**\n\n")

    def _format_commanders(commanders_list):
        return "\n".join(commanders_list) if commanders_list else es("*Не назначен*")

    num_games = record.get('num_games', 0)
    if num_games == 0:
        players = record.get('overall_players', [])
        commanders = _normalize_commanders_field(record, 'overall_commander', 'overall_commanders')
        side_commander = record.get('overall_side_commander')

        report_text += es(f"👥 **Явились на мероприятие ({len(players)}):**\n")
        report_text += "\n".join(players) if players else es("*Никто не явился*")
        report_text += "\n\n" + es("🪖 **Командир(ы) отделения:**\n") + _format_commanders(commanders)
        report_text += "\n\n" + es("🎖️ **Командир стороны:**\n")
        report_text += side_commander if side_commander else es("*Не назначен*")
    else:
        games = record.get('games', {}) or {}
        for i in range(num_games):
            g = games.get(str(i + 1), {})
            players = g.get('players', [])
            commanders = _normalize_commanders_field(g, 'commander', 'commanders')
            side_commander = g.get('side_commander')

            report_text += es(f"🎮 **Игра {i+1}**\n\n")
            report_text += es(f"👥 Явились ({len(players)}):\n")
            report_text += "\n".join(players) if players else es("*Никто не явился*")
            report_text += "\n\n" + es(f"🪖 Командир(ы) отделения:\n") + _format_commanders(commanders)
            report_text += "\n\n" + es(f"🎖️ Командир стороны:\n")
            report_text += side_commander if side_commander else es("*Не назначен*")
            if i < num_games - 1:
                report_text += "\n\n"
    return report_text


async def lock_and_archive_thread(thread):
    """Закрывает и блокирует ветку обсуждения (locked+archived)."""
    if not thread:
        return
    try:
        await thread.edit(locked=True, archived=True)
    except Exception as e:
        print(f"⚠️ Не удалось закрыть/заблокировать ветку {thread.id}: {e}")


async def unlock_and_unarchive_thread(thread):
    """Открывает ветку перед публикацией сообщения, если она была
    заблокирована/заархивирована ранее (например, отчёт о явке подаётся
    повторно уже после автоматического завершения мероприятия)."""
    if not thread:
        return
    try:
        if getattr(thread, 'locked', False) or getattr(thread, 'archived', False):
            await thread.edit(locked=False, archived=False)
    except Exception as e:
        print(f"⚠️ Не удалось открыть ветку {thread.id}: {e}")


def record_thread_message(event: dict, message_id: int, kind: str, mention_block: str = "", extra: dict = None):
    """Регистрирует сообщение, отправленное ботом в ветку мероприятия, чтобы
    функция синхронизации могла впоследствии найти и переформатировать его."""
    event.setdefault('thread_messages', [])
    event['thread_messages'].append({
        'id': message_id, 'kind': kind,
        'mention_block': mention_block, 'extra': extra or {}
    })


# --- Единые шаблоны сообщений (используются и при отправке, и при синхронизации) ---

def render_announcement_message(mention_block: str) -> str:
    return f"{mention_block}\n\n" + es("📢 Бойцы, запланировано мероприятие! Ждем ваших отметок!")

def render_completion_message(event: dict) -> str:
    return es(f"🏁 Мероприятие «{clean_event_title(event['title'])}» автоматически помечено как завершённое.")

def render_early_completion_message(event: dict) -> str:
    return es(f"🏁 Мероприятие «{clean_event_title(event['title'])}» завершено досрочно после публикации отчёта о явке.")

def render_cancel_message(event: dict, by_user: str) -> str:
    return es(f"🚫 Мероприятие «{clean_event_title(event['title'])}» отменено командованием ({by_user}).")

def render_reactivate_message(event: dict) -> str:
    return es(f"🔄 Мероприятие «{clean_event_title(event['title'])}» снова активно.")

def render_reminder_2days_message(mention_block: str) -> str:
    return (mention_block + "\n\n" +
        "Бойцы, ждём ваших отметок! До мероприятия осталось 2 суток, но вы пока ещё не отметились! " +
        f"Пожалуйста, отметьтесь в основном посте в <#{EVENTS_CHANNEL_ID}>.")

def render_reminder_15min_message(mention_block: str, event: dict) -> str:
    start_ts = int(event['start_time'])
    return (mention_block + "\n\n" +
        es("📢 Бойцы, внимание!") + "\n\n" +
        f"Мероприятие начнется <t:{start_ts}:R>! Ждем вас на сборах! Заходите в голосовой канал <#{VOICE_CHANNEL_ID}>.")

def render_mods_message(mention_block: str, event: dict, server_name: str, password: str) -> str:
    start_ts = int(event['start_time'])
    text = mention_block + "\n\n" + es("📢 Бойцы, внимание!") + "\n\n"
    if server_name:
        text += f"Сервер: {server_name}\n\n"
    text += f"Мероприятие начнется <t:{start_ts}:R>! Моды уже можно начать скачивать!"
    if password:
        text += f" Пароль: {password}"
    return text


class AttendanceWizard:
    def __init__(self, event_id, num_games, event_title):
        self.event_id = event_id
        self.num_games = num_games
        self.event_title = event_title
        self.data = {}
        self.commanders = {}
        self.side_commanders = {}
        self.current_step = 0
        self.phase = 'players'


class CommandersSelectView(discord.ui.View):
    """Выбор командира(ов) отделения и командира стороны — строго из явившихся
    на этом шаге/игре. Оба поля необязательны: если ничего не выбрано —
    считается, что командир не назначен (без отдельного пункта 'Без командира')."""
    def __init__(self, wizard, present_players):
        super().__init__(timeout=300)
        self.wizard = wizard
        self.squad_commanders = []
        self.side_commander = None

        capped_players = present_players[:MAX_SELECT_OPTIONS]

        if capped_players:
            squad_options = [discord.SelectOption(label=nick, value=nick) for nick in capped_players]
            squad_disabled = False
        else:
            squad_options = [discord.SelectOption(label="— Нет явившихся —", value="__none__")]
            squad_disabled = True

        self.squad_select = discord.ui.Select(
            placeholder="🪖 Командир(ы) отделения (можно несколько, необязательно)...",
            options=squad_options, min_values=0, max_values=len(squad_options), row=0,
            disabled=squad_disabled
        )
        self.squad_select.callback = self._squad_callback
        self.add_item(self.squad_select)

        if capped_players:
            side_options = [discord.SelectOption(label=nick, value=nick) for nick in capped_players]
            side_disabled = False
        else:
            side_options = [discord.SelectOption(label="— Нет явившихся —", value="__none__")]
            side_disabled = True

        self.side_select = discord.ui.Select(
            placeholder="🎖️ Командир стороны (необязательно)...",
            options=side_options, min_values=0, max_values=1, row=1,
            disabled=side_disabled
        )
        self.side_select.callback = self._side_callback
        self.add_item(self.side_select)

        continue_btn = discord.ui.Button(label=es("➡️ Продолжить"), style=discord.ButtonStyle.primary, row=2)
        continue_btn.callback = self._continue_callback
        self.add_item(continue_btn)

    async def _squad_callback(self, interaction):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Только комбат или заместитель!"), ephemeral=True)
            return
        self.squad_commanders = [v for v in self.squad_select.values if v != "__none__"]
        await interaction.response.defer()

    async def _side_callback(self, interaction):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Только комбат или заместитель!"), ephemeral=True)
            return
        values = [v for v in self.side_select.values if v != "__none__"]
        self.side_commander = values[0] if values else None
        await interaction.response.defer()

    async def _continue_callback(self, interaction):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Только комбат или заместитель!"), ephemeral=True)
            return
        key = "overall" if self.wizard.num_games == 0 else self.wizard.current_step
        self.wizard.commanders[key] = list(self.squad_commanders)
        self.wizard.side_commanders[key] = self.side_commander
        self.stop()
        await interaction.response.defer()
        await proceed_to_next_step(interaction, self.wizard)

class AttendanceStepView(discord.ui.View):
    def __init__(self, wizard, step, clan_members):
        super().__init__(timeout=300)
        self.wizard = wizard
        self.step = step
        self.selects = []
        
        parts = []
        for i in range(0, len(clan_members), MAX_SELECT_OPTIONS):
            parts.append(clan_members[i:i+MAX_SELECT_OPTIONS])
        
        for idx, part in enumerate(parts):
            label_suffix = f" (ч. {idx+1}/{len(parts)})" if len(parts) > 1 else ""
            options = [discord.SelectOption(label=nick, value=nick) for nick in part]
            select = discord.ui.Select(
                placeholder=f"👥 Выберите явившихся бойцов{label_suffix}...",
                options=options,
                min_values=0,
                max_values=len(part),
                custom_id=f"attendance_select_{step}_{idx}"
            )
            select.callback = self._make_select_callback(select)
            self.add_item(select)
            self.selects.append(select)
        
        if step == 0 and wizard.num_games == 0:
            finish_btn = discord.ui.Button(label=es("➡️ Далее (командир отделения)"), style=discord.ButtonStyle.primary, custom_id=f"attendance_to_commander_{step}", row=4)
            finish_btn.callback = self.to_commander_callback
            self.add_item(finish_btn)
        elif wizard.num_games > 1 and step < wizard.num_games - 1:
            skip_btn = discord.ui.Button(label=es("⏭️ Пропустить этот матч"), style=discord.ButtonStyle.secondary, custom_id=f"attendance_skip_{step}", row=4)
            skip_btn.callback = self.skip_callback
            self.add_item(skip_btn)
            next_btn = discord.ui.Button(label=es(f"➡️ Далее (командир отделения)"), style=discord.ButtonStyle.primary, custom_id=f"attendance_next_{step}", row=4)
            next_btn.callback = self.to_commander_callback
            self.add_item(next_btn)
        else:
            skip_btn = discord.ui.Button(label=es("⏭️ Пропустить этот матч"), style=discord.ButtonStyle.secondary, custom_id=f"attendance_skip_{step}", row=4)
            skip_btn.callback = self.skip_callback
            self.add_item(skip_btn)
            next_btn = discord.ui.Button(label=es(f"➡️ Далее (командир отделения)"), style=discord.ButtonStyle.primary, custom_id=f"attendance_next_{step}", row=4)
            next_btn.callback = self.to_commander_callback
            self.add_item(next_btn)
    
    def _make_select_callback(self, select):
        async def callback(interaction):
            if interaction.user.id not in ADMIN_USER_IDS:
                await interaction.response.send_message(es("⛔ Только комбат или заместитель!"), ephemeral=True)
                return
            await interaction.response.defer()
        return callback
    
    def _collect_all_selected(self):
        all_selected = []
        for select in self.selects:
            for value in select.values:
                if value not in all_selected:
                    all_selected.append(value)
        return all_selected
    
    async def skip_callback(self, interaction):
        if interaction.user.id not in ADMIN_USER_IDS:
            return
        if self.wizard.num_games == 0:
            self.wizard.data["overall"] = []
        else:
            self.wizard.data[self.step] = []
        self.stop()
        await interaction.response.defer()
        await show_commander_step(interaction, self.wizard)
    
    async def to_commander_callback(self, interaction):
        if interaction.user.id not in ADMIN_USER_IDS:
            return
        selected = self._collect_all_selected()
        if self.wizard.num_games == 0:
            self.wizard.data["overall"] = selected
        else:
            self.wizard.data[self.step] = selected
        self.stop()
        await interaction.response.defer()
        await show_commander_step(interaction, self.wizard)


async def start_attendance_wizard(interaction, event_id):
    events = load_json(EVENTS_FILE, {})
    event = events.get(event_id)
    if not event:
        await interaction.response.send_message(es("❌ Мероприятие не найдено!"), ephemeral=True)
        return
    num_games = event.get('num_games', 0)
    wizard = AttendanceWizard(event_id, num_games, event.get('title', ''))
    clan_members = await get_active_members(datetime.now(MSK))
    if not clan_members:
        await interaction.response.send_message(es("❌ Список клана пуст!"), ephemeral=True)
        return

    attendance = load_json(ATTENDANCE_FILE, {})
    warning_prefix = ""
    if event_id in attendance:
        warning_prefix = (
            es("⚠️ **Внимание: отчёт о явке для этого мероприятия уже существует!**\n") +
            "Заполнение нового отчёта **полностью перезапишет** предыдущий — включая пересчёт "
            "зачтённых отыгрышей и очереди на командование (старые значения будут отменены, новые применены).\n\n"
        )

    view = AttendanceStepView(wizard, 0, clan_members)
    if num_games == 0:
        title_text = warning_prefix + es(f"👥 **{event.get('title', '')}**\n\n") + es("Выберите бойцов, явившихся на мероприятие:")
    else:
        title_text = warning_prefix + es(f"👥 **{event.get('title', '')}**\n\n") + es(f"**Матч 1** из {num_games}\nВыберите явившихся:")
    await interaction.response.send_message(title_text, view=view, ephemeral=True)


async def show_commander_step(interaction, wizard):
    key = "overall" if wizard.num_games == 0 else wizard.current_step
    present_players = wizard.data.get(key, [])
    view = CommandersSelectView(wizard, present_players)

    if wizard.num_games == 0:
        title_text = es(f"🪖 **{wizard.event_title}**\n\n") + es("Выберите командира отделения и командира стороны (из числа явившихся):")
    else:
        title_text = es(f"🪖 **{wizard.event_title}**\n\n") + es(f"**Командиры на матче {wizard.current_step + 1}** из {wizard.num_games}\nВыберите из числа явившихся на этот матч:")

    await interaction.followup.send(title_text, view=view, ephemeral=True)

async def proceed_to_next_step(interaction, wizard):
    clan_members = await get_active_members(datetime.now(MSK))
    
    if wizard.num_games == 0:
        await finalize_attendance(interaction, wizard)
        return
    
    wizard.current_step += 1
    
    if wizard.current_step >= wizard.num_games:
        await finalize_attendance(interaction, wizard)
        return
    
    view = AttendanceStepView(wizard, wizard.current_step, clan_members)
    title_text = es(f"👥 **{wizard.event_title}**\n\n") + es(f"**Матч {wizard.current_step + 1}** из {wizard.num_games}\nВыберите явившихся:")
    await interaction.followup.send(title_text, view=view, ephemeral=True)


async def finalize_attendance(interaction, wizard):
    events = load_json(EVENTS_FILE, {})
    event = events.get(wizard.event_id)
    if not event:
        await interaction.followup.send(es("❌ Мероприятие не найдено!"), ephemeral=True)
        return
    
    attendance = load_json(ATTENDANCE_FILE, {})
    old_record = attendance.get(wizard.event_id)

    record['thread_id'] = thread.id

    # Если ветка уже закрыта (мероприятие ранее было автоматически завершено) —
    # открываем её на время публикации отчёта, заблокируем обратно ниже.
    await unlock_and_unarchive_thread(thread)

    if old_record:
        if old_record.get('attendance_message_id') and old_record.get('thread_id'):
            try:
                old_thread = await client.fetch_channel(old_record['thread_id'])
                old_msg = await old_thread.fetch_message(old_record['attendance_message_id'])
                await old_msg.delete()
                print(f"🗑️ Удалено старое сообщение явки для '{wizard.event_title}'")
            except Exception:
                pass
    
    event_start = datetime.fromtimestamp(event['start_time'], MSK)
    record = {
        'event_id': wizard.event_id,
        'title': wizard.event_title,
        'date': event_start.strftime('%d.%m.%Y %H:%M'),
        'event_message_id': event.get('message_id'),
        'event_channel_id': event.get('channel_id'),
        'reported_at': datetime.now(MSK).isoformat(),
        'reported_by': interaction.user.display_name,
        'num_games': wizard.num_games
    }
    
    if wizard.num_games == 0:
        record['overall_players'] = wizard.data.get('overall', [])
        record['overall_commanders'] = wizard.commanders.get('overall') or []
        record['overall_side_commander'] = wizard.side_commanders.get('overall')
    else:
        record['games'] = {}
        for i in range(wizard.num_games):
            record['games'][str(i+1)] = {
                'players': wizard.data.get(i, []),
                'commanders': wizard.commanders.get(i) or [],
                'side_commander': wizard.side_commanders.get(i)
            }
    
    thread = await get_or_create_thread(event, wizard.event_id, wizard.event_title)
    if not thread:
        await interaction.followup.send(es("❌ Не удалось получить ветку мероприятия!"), ephemeral=True)
        return
    
    record['thread_id'] = thread.id
    
    report_text = build_attendance_report_text(record)
    
    new_msg = await thread.send(report_text)
    record['attendance_message_id'] = new_msg.id
    
    attendance[wizard.event_id] = record
    save_json(ATTENDANCE_FILE, attendance)
    
    await apply_attendance_to_gamestats(wizard, old_record=old_record)

    queue_changed = await apply_squad_commander_queue_promotion(wizard, old_record=old_record)
    if queue_changed:
        await refresh_all_active_event_embeds()

    # === Завершение мероприятия при подаче явки (п.1, п.2) ===
    event_end_dt = datetime.fromtimestamp(event['end_time'], MSK)
    now = datetime.now(MSK)
    events_fresh = load_json(EVENTS_FILE, {})
    fresh_event = events_fresh.get(wizard.event_id)
    if fresh_event and fresh_event.get('status') != 'completed':
        was_early = now < event_end_dt
        fresh_event['status'] = 'completed'
        save_json(EVENTS_FILE, events_fresh)
        await refresh_event_message(wizard.event_id)
        try:
            await thread.edit(name=desired_thread_name(fresh_event))
        except Exception:
            pass
        if was_early:
            try:
                msg = await thread.send(render_early_completion_message(fresh_event))
                record_thread_message(fresh_event, msg.id, 'early_completion')
                save_json(EVENTS_FILE, events_fresh)
            except Exception:
                pass
        await process_inactivity_discipline_for_event(wizard.event_id)

    # Ветка закрывается и блокируется в ЛЮБОМ случае — раз отчёт о явке опубликован (п.2)
    await lock_and_archive_thread(thread)

    await interaction.followup.send(es("✅ Отчёт о явке опубликован в ветке мероприятия!"), ephemeral=True)
    print(f"✅ Отчёт о явке для '{wizard.event_title}' опубликован")


# ============== ФУНКЦИИ ОТПУСКОВ ==============

async def publish_vacation_info(interaction):
    try:
        channel = await client.fetch_channel(VACATION_CHANNEL_ID)
        embed = discord.Embed(title=es("🏖️ Оформление отпусков"), description=VACATION_RULES, color=discord.Color.green())
        embed.set_footer(text="Нажмите кнопку ниже, чтобы оформить отпуск")
        await channel.send(embed=embed, view=VacationRequestView())
        await interaction.response.send_message(es("✅ Правила отпусков опубликованы!"), ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)


async def handle_vacation_request(interaction, nickname, start_str, end_str, reason, by_admin):
    try:
        start_date = datetime.strptime(start_str, "%d.%m.%Y")
        end_date = datetime.strptime(end_str, "%d.%m.%Y")
        duration = (end_date - start_date).days
        if duration < 7:
            await interaction.response.send_message(es("❌ Отпуск должен быть не менее 7 дней!"), ephemeral=True)
            return
        if duration > 31:
            await interaction.response.send_message(es("❌ Отпуск не может быть дольше 31 дня!"), ephemeral=True)
            return
        if not by_admin and start_date.date() < datetime.now(MSK).date():
            await interaction.response.send_message(es("❌ Дата начала должна быть в будущем!"), ephemeral=True)
            return
        member = await find_member_by_nickname(nickname)
        if not member:
            await interaction.response.send_message(f"❌ Боец {nickname} не найден!", ephemeral=True)
            return
        vacations = load_json(VACATIONS_FILE, {})
        if nickname in vacations and vacations[nickname].get('status') in ['active', 'pending']:
            await interaction.response.send_message(f"⚠️ У {nickname} уже есть отпуск!", ephemeral=True)
            return
        vacations[nickname] = {
            'start': start_date.isoformat(),
            'end': end_date.isoformat(),
            'reason': reason,
            'requested_at': datetime.now(MSK).isoformat(),
            'status': 'pending',
            'message_id': None,
            'channel_id': None,
            'thread_id': None,
            'created_by': interaction.user.display_name if by_admin else 'Сам боец',
            'by_admin': by_admin
        }
        save_json(VACATIONS_FILE, vacations)
        channel = await client.fetch_channel(VACATION_CHANNEL_ID)
        embed_description = f"Отпуск для **{nickname}**" if by_admin else f"**{nickname}** запросил(а) отпуск"
        embed = discord.Embed(title=es("🏖️ Отпуск требует утверждения"), description=embed_description, color=discord.Color.orange())
        embed.add_field(
            name=es("📅 Период"),
            value=format_vacation_period(start_date.isoformat(), end_date.isoformat()),
            inline=False
        )
        embed.add_field(name=es("📝 Причина"), value=reason, inline=False)
        if by_admin:
            embed.add_field(name=es("👤 Оформил"), value=f"Комбат или заместитель: {interaction.user.display_name}", inline=False)
        else:
            embed.add_field(name=es("👤 Запросил"), value=interaction.user.display_name, inline=False)
        embed.add_field(name=es("ℹ️ Статус"), value="Ожидает утверждения комбатом", inline=False)
        embed.set_footer(text="Комбат или заместитель: утвердите или отклоните отпуск")
        message = await channel.send(embed=embed, view=VacationApprovalView())
        vacations[nickname]['message_id'] = message.id
        vacations[nickname]['channel_id'] = channel.id
        try:
            thread = await message.create_thread(name=f"💬 Утверждение отпуска - {nickname}")
            guild = channel.guild
            mention_block = get_leadership_mentions(guild, 'kombat_arma', 'zam_kombat_arma')
            vacation_mention = f"<#{VACATION_CHANNEL_ID}>"
            if mention_block:
                await thread.send(f"{mention_block}\n\n" + es(f"Новый запрос на отпуск от **{nickname}**! Перейдите в канал {vacation_mention} и рассмотрите рапорт."))
            else:
                await thread.send(es(f"Новый запрос на отпуск от **{nickname}**! Перейдите в канал {vacation_mention}."))
            vacations[nickname]['thread_id'] = thread.id
        except Exception:
            pass
        save_json(VACATIONS_FILE, vacations)
        await interaction.response.send_message(es("✅ Запрос на отпуск отправлен!"), ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)


async def approve_vacation(interaction, nickname):
    vacations = load_json(VACATIONS_FILE, {})
    if nickname not in vacations:
        await interaction.response.send_message(es("❌ Отпуск не найден!"), ephemeral=True)
        return
    vacation = vacations[nickname]
    if vacation.get('status') != 'pending':
        await interaction.response.send_message(es("⚠️ Отпуск уже обработан!"), ephemeral=True)
        return
    vacation['status'] = 'active'
    vacation['approved_at'] = datetime.now(MSK).isoformat()
    vacation['approved_by'] = interaction.user.display_name
    save_json(VACATIONS_FILE, vacations)
    member = await find_member_by_nickname(nickname)
    if member:
        await update_vacation_role(member, True)
    try:
        channel = await client.fetch_channel(vacation['channel_id'])
        message = await channel.fetch_message(vacation['message_id'])
        if message.embeds:
            embed = message.embeds[0]
            embed.color = discord.Color.green()
            embed.title = es("🏖️ Отпуск утверждён")
            embed.description = f"Отпуск для **{nickname}**" if vacation.get('by_admin') else f"**{nickname}** взял отпуск"
            # ВАЖНО: без break, чтобы обновлялись ОБА поля (и Период, и Статус)
            for i, field in enumerate(embed.fields):
                if field.name == es("📅 Период"):
                    embed.set_field_at(i, name=es("📅 Период"), value=format_vacation_period(vacation['start'], vacation['end']), inline=False)
                elif field.name == es("ℹ️ Статус"):
                    embed.set_field_at(i, name=es("ℹ️ Статус"), value="Утверждён и активен", inline=False)
            embed.add_field(name=es("✅ Утвердил"), value=interaction.user.display_name, inline=False)
            embed.set_footer(text="Во время отпуска вам не нужно отмечаться в расписании мероприятий")
            await message.edit(embed=embed, view=VacationMessageView())
    except Exception:
        pass
    await interaction.response.send_message(f"✅ Отпуск {nickname} утверждён!", ephemeral=True)


async def reject_vacation(interaction, nickname):
    vacations = load_json(VACATIONS_FILE, {})
    if nickname not in vacations:
        await interaction.response.send_message(es("❌ Отпуск не найден!"), ephemeral=True)
        return
    vacation = vacations[nickname]
    if vacation.get('status') != 'pending':
        await interaction.response.send_message(es("⚠️ Отпуск уже обработан!"), ephemeral=True)
        return
    vacation['status'] = 'rejected'
    vacation['rejected_at'] = datetime.now(MSK).isoformat()
    vacation['rejected_by'] = interaction.user.display_name
    save_json(VACATIONS_FILE, vacations)
    try:
        channel = await client.fetch_channel(vacation['channel_id'])
        message = await channel.fetch_message(vacation['message_id'])
        if message.embeds:
            embed = message.embeds[0]
            embed.color = discord.Color.red()
            embed.title = es("❌ Отпуск отклонён")
            embed.description = f"Отпуск для **{nickname}** отклонён" if vacation.get('by_admin') else f"Запрос на отпуск **{nickname}** отклонён"
            for i, field in enumerate(embed.fields):
                if field.name == es("ℹ️ Статус"):
                    embed.set_field_at(i, name=es("ℹ️ Статус"), value="Отклонён командованием", inline=False)
                    break
            embed.add_field(name=es("❌ Отклонил"), value=interaction.user.display_name, inline=False)
            embed.set_footer(text="Отпуск аннулирован.")
            await message.edit(embed=embed, view=None)
    except Exception:
        pass
    await interaction.response.send_message(f"❌ Отпуск {nickname} отклонён.", ephemeral=True)


async def close_vacation(interaction, nickname, early=False, by_admin=False):
    vacations = load_json(VACATIONS_FILE, {})
    if nickname not in vacations:
        await interaction.response.send_message(es("❌ Отпуск не найден!"), ephemeral=True)
        return
    vacation = vacations[nickname]
    if vacation.get('status') != 'active':
        await interaction.response.send_message(es("⚠️ Отпуск уже закрыт!"), ephemeral=True)
        return

    # Сразу подтверждаем interaction, ДО любых медленных сетевых операций
    # (поиск участника гильдии, отправка DM, редактирование embed'а) —
    # иначе при малейшей задержке Discord API исходная interaction "протухает"
    # за 3 секунды и финальный ответ падает с 10062 Unknown interaction.
    await interaction.response.defer(ephemeral=True, thinking=True)

    vacation['status'] = 'ended_early' if early else 'ended_scheduled'
    vacation['closed_at'] = datetime.now(MSK).isoformat()
    vacation['closed_by'] = interaction.user.display_name
    save_json(VACATIONS_FILE, vacations)
    member = await find_member_by_nickname(nickname)
    if member:
        await update_vacation_role(member, False)
    await send_vacation_return_message(nickname)
    try:
        channel = await client.fetch_channel(vacation['channel_id'])
        message = await channel.fetch_message(vacation['message_id'])
        if message.embeds:
            embed = message.embeds[0]
            status_text = "Завершен досрочно" if early else "Завершен по истечению срока"
            for i, field in enumerate(embed.fields):
                if field.name == es("ℹ️ Статус"):
                    embed.set_field_at(i, name=es("ℹ️ Статус"), value=status_text, inline=False)
                    break
                elif field.name == es("📅 Период"):
                    embed.set_field_at(i, name=es("📅 Период"), value=format_vacation_period(vacation['start'], vacation['end']), inline=False)
            embed.color = discord.Color.red() if early else discord.Color.greyple()
            await message.edit(embed=embed, view=None)
    except Exception:
        pass
    await interaction.followup.send(f"✅ Отпуск {nickname} закрыт.", ephemeral=True)


async def show_vacation_list(interaction):
    vacations = load_json(VACATIONS_FILE, {})
    active = {k: v for k, v in vacations.items() if v.get('status') == 'active'}
    pending = {k: v for k, v in vacations.items() if v.get('status') == 'pending'}
    if not active and not pending:
        await interaction.response.send_message(es("🏖️ Нет отпусков"), ephemeral=True)
        return
    text = ""
    if pending:
        text += es("⏳ **Ожидают утверждения:**\n\n")
        for nickname, data in pending.items():
            start = datetime.fromisoformat(data['start']).strftime('%d.%m.%Y')
            end = datetime.fromisoformat(data['end']).strftime('%d.%m.%Y')
            text += f"**{nickname}**: {start} - {end}\n"
            text += f"Причина: {data.get('reason', 'Не указана')}\n\n"
    if active:
        text += es("✅ **Активные отпуска:**\n\n")
        for nickname, data in active.items():
            start = datetime.fromisoformat(data['start']).strftime('%d.%m.%Y')
            end = datetime.fromisoformat(data['end']).strftime('%d.%m.%Y')
            text += f"**{nickname}**: {start} - {end}\n"
            text += f"Причина: {data.get('reason', 'Не указана')}\n\n"
    await interaction.response.send_message(text, ephemeral=True)


async def check_expired_vacations():
    vacations = load_json(VACATIONS_FILE, {})
    current_date = datetime.now(MSK).date()
    changed = False
    for nickname, data in vacations.items():
        if data.get('status') != 'active':
            continue
        try:
            end_date = datetime.fromisoformat(data['end']).date()
            if end_date < current_date:
                data['status'] = 'ended_scheduled'
                data['closed_at'] = datetime.now(MSK).isoformat()
                data['closed_by'] = 'Система (автоматически)'
                changed = True
                member = await find_member_by_nickname(nickname)
                if member:
                    await update_vacation_role(member, False)
                if data.get('message_id') and data.get('channel_id'):
                    try:
                        channel = await client.fetch_channel(data['channel_id'])
                        message = await channel.fetch_message(data['message_id'])
                        if message.embeds:
                            embed = message.embeds[0]
                            for i, field in enumerate(embed.fields):
                                if field.name == es("ℹ️ Статус"):
                                    embed.set_field_at(i, name=es("ℹ️ Статус"), value="Завершен по истечению срока", inline=False)
                                elif field.name == es("📅 Период"):
                                    embed.set_field_at(i, name=es("📅 Период"), value=format_vacation_period(data['start'], data['end']), inline=False)
                            embed.color = discord.Color.greyple()
                            await message.edit(embed=embed, view=None)
                    except Exception:
                        pass
                print(f"✅ Отпуск {nickname} автоматически закрыт (истёк срок)")
                await send_vacation_return_message(nickname)
        except Exception:
            pass
    if changed:
        save_json(VACATIONS_FILE, vacations)
        
async def check_vacation_ending_soon():
    """Отправляет напоминание бойцу за сутки до окончания его отпуска (один раз)."""
    vacations = load_json(VACATIONS_FILE, {})
    current_time = datetime.now(MSK)
    changed = False
    for nickname, data in vacations.items():
        if data.get('status') != 'active':
            continue
        if data.get('ending_reminder_sent'):
            continue
        try:
            end_date = datetime.fromisoformat(data['end']).date()
            end_of_day = MSK.localize(datetime.combine(end_date, datetime.min.time())) + timedelta(hours=23, minutes=59)
            time_left = end_of_day - current_time
            if timedelta(0) <= time_left <= timedelta(hours=24):
                member = await find_member_by_nickname(nickname)
                reminder_text = (
                    es("🏖️ **Напоминание об отпуске**\n\n") +
                    f"Ваш отпуск заканчивается завтра, {end_date.strftime('%d.%m.%Y')}.\n\n" +
                    "Если вам нужно продлить отпуск, создайте новый рапорт, а если не нужно, то с возвращением в ряды!."
                )
                sent = False
                if member:
                    try:
                        await member.send(reminder_text)
                        sent = True
                    except Exception:
                        pass
                if not sent and data.get('thread_id'):
                    try:
                        thread = await client.fetch_channel(data['thread_id'])
                        mention = member.mention if member else f"**{nickname}**"
                        await thread.send(f"{mention}\n\n" + reminder_text)
                    except Exception:
                        pass
                data['ending_reminder_sent'] = True
                changed = True
        except Exception:
            continue
    if changed:
        save_json(VACATIONS_FILE, vacations)


async def send_vacation_return_message(nickname):
    """Отправляет бойцу сообщение о том, что его отпуск завершён — с возвращением в ряды."""
    vacations = load_json(VACATIONS_FILE, {})
    data = vacations.get(nickname, {})
    member = await find_member_by_nickname(nickname)
    text = es("🏖️ **Напоминание об отпуске!**\n\n") + "Ваш отпуск завершён. Рады видеть вас снова в строю!"
    sent = False
    if member:
        try:
            await member.send(text)
            sent = True
        except Exception:
            pass
    if not sent and data.get('thread_id'):
        try:
            thread = await client.fetch_channel(data['thread_id'])
            mention = member.mention if member else f"**{nickname}**"
            await thread.send(f"{mention}\n\n" + text)
        except Exception:
            pass


# ============== ФУНКЦИИ МЕРОПРИЯТИЙ ==============

async def handle_event_response(interaction, event_id, response_type):
    events = load_json(EVENTS_FILE, {})
    if event_id not in events:
        await interaction.response.send_message(es("❌ Мероприятие не найдено!"), ephemeral=True)
        return
    event = events[event_id]
    if event.get('status', 'active') != 'active':
        await interaction.response.send_message(es("⛔ Мероприятие отменено или уже завершено. Отметки не принимаются!"), ephemeral=True)
        return
    nickname = interaction.user.display_name
    current_date = datetime.now(MSK)
    event_end = datetime.fromtimestamp(event['end_time'], MSK)
    if current_date > event_end:
        await interaction.response.send_message(es("⛔ Мероприятие уже завершено. Отметки больше не принимаются!"), ephemeral=True)
        return

    # === Запрет на участие, если бойца нет в списке клана (только в режиме Firebase) (п.9) ===
    # В аварийном режиме DATA_BACKEND='json' проверка членства недоступна —
    # список бойцов физически хранится в Firebase, а не в наших JSON-файлах,
    # поэтому гейт сознательно отключается, чтобы не заблокировать ВСЕХ бойцов
    # подряд из-за отсутствия данных.
    if USE_FIREBASE_BACKEND:
        clan_members = await load_clan_members_from_firebase()
        if nickname not in clan_members:
            await interaction.response.send_message(
                es("⛔ Вы не найдены в списке клана. Обратитесь к командованию, чтобы отметиться."),
                ephemeral=True
            )
            return

    if is_on_vacation_dynamic(nickname, current_date):
        await interaction.response.send_message(es("🏖️ Вы сейчас в отпуске."), ephemeral=True)
        return
    if response_type == "accept":
        event['accepted'][nickname] = True
        event['declined'].pop(nickname, None)
        await interaction.response.send_message(es("✅ Вы записаны на мероприятие!"), ephemeral=True)
    else:
        event['declined'][nickname] = True
        event['accepted'].pop(nickname, None)
        await interaction.response.send_message(es("❌ Вы отказались от участия!"), ephemeral=True)
    save_json(EVENTS_FILE, events)
    await refresh_event_message(event_id)


async def open_edit_modal(interaction, event_id, image_key=None, num_games=None, mandatory=None):
    events = load_json(EVENTS_FILE, {})
    if event_id not in events:
        await interaction.response.send_message(es("❌ Мероприятие не найдено!"), ephemeral=True)
        return
    event = events[event_id]
    if image_key is None or image_key == "__keep__":
        image_key = event.get('image_key', 'none')
    if num_games is None:
        num_games = event.get('num_games', 0)
    if mandatory is None:
        mandatory = event.get('mandatory', True)

    start_dt = datetime.fromtimestamp(event['start_time'], MSK)
    end_dt = datetime.fromtimestamp(event['end_time'], MSK)
    await interaction.response.send_modal(EventEditModal(
        event_id=event_id,
        current_title=clean_event_title(event['title']),
        current_description=event['description'],
        current_date=start_dt.strftime("%d.%m.%Y"),
        current_start_time=start_dt.strftime("%H:%M"),
        current_end_time=end_dt.strftime("%H:%M"),
        image_key=image_key, num_games=num_games, mandatory=mandatory
    ))


async def update_event(event_id, title, description, start_time, end_time, image_key=None, num_games=None, mandatory=None):
    events = load_json(EVENTS_FILE, {})
    if event_id not in events:
        return
    event = events[event_id]
    event['title'] = title
    event['description'] = description
    event['start_time'] = int(start_time.timestamp())
    event['end_time'] = int(end_time.timestamp())
    became_active_again = (event.get('status') == 'completed' and event['end_time'] > int(datetime.now(MSK).timestamp()))
    if became_active_again:
        event['status'] = 'active'
        event['discipline_processed'] = False
    if image_key is not None:
        event['image_key'] = image_key
    if num_games is not None:
        event['num_games'] = num_games
    if mandatory is not None:
        event['mandatory'] = mandatory
    event['reminder_2days_sent'] = False
    event['reminder_15min_sent'] = False
    save_json(EVENTS_FILE, events)
    await refresh_event_message(event_id)
    if became_active_again and event.get('thread_id'):
        try:
            thread = await client.fetch_channel(event['thread_id'])
            await unlock_and_unarchive_thread(thread)
        except Exception:
            pass


async def cancel_event(interaction, event_id):
    """Отменяет мероприятие: убирает кнопки Приду/Не приду, меняет статус.
    Данные НЕ удаляются (в отличие от delete_event) — можно активировать снова."""
    events = load_json(EVENTS_FILE, {})
    if event_id not in events:
        await interaction.response.send_message(es("❌ Мероприятие не найдено!"), ephemeral=True)
        return
    event = events[event_id]
    if event.get('status') == 'cancelled':
        await interaction.response.send_message(es("⚠️ Мероприятие уже отменено!"), ephemeral=True)
        return
    event['status'] = 'cancelled'
    save_json(EVENTS_FILE, events)
    await refresh_event_message(event_id)
    if event.get('thread_id'):
        try:
            thread = await client.fetch_channel(event['thread_id'])
            await unlock_and_unarchive_thread(thread)
            await thread.edit(name=desired_thread_name(event))
            msg = await thread.send(render_cancel_message(event, interaction.user.display_name))
            record_thread_message(event, msg.id, 'cancelled', extra={'by_user': interaction.user.display_name})
            save_json(EVENTS_FILE, events)
        except Exception:
            pass
    await interaction.response.send_message(es("✅ Мероприятие отменено! Данные сохранены, его можно снова активировать."), ephemeral=True)


async def reactivate_event(interaction, event_id):
    """Возвращает отменённое мероприятие обратно в активное состояние (п.4)."""
    events = load_json(EVENTS_FILE, {})
    if event_id not in events:
        await interaction.response.send_message(es("❌ Мероприятие не найдено!"), ephemeral=True)
        return
    event = events[event_id]
    if event.get('status') != 'cancelled':
        await interaction.response.send_message(es("⚠️ Мероприятие не отменено, реактивация не требуется!"), ephemeral=True)
        return
    event_end = datetime.fromtimestamp(event['end_time'], MSK)
    event['status'] = 'completed' if datetime.now(MSK) > event_end else 'active'
    save_json(EVENTS_FILE, events)
    await refresh_event_message(event_id)
    if event.get('thread_id'):
        try:
            thread = await client.fetch_channel(event['thread_id'])
            await unlock_and_unarchive_thread(thread)
            await thread.edit(name=desired_thread_name(event))
            msg = await thread.send(render_reactivate_message(event))
            record_thread_message(event, msg.id, 'reactivated')
            save_json(EVENTS_FILE, events)
            if event['status'] == 'completed':
                await lock_and_archive_thread(thread)
        except Exception:
            pass
    await interaction.response.send_message(es("✅ Мероприятие активировано снова!"), ephemeral=True)


async def delete_event(interaction, event_id):
    """Полностью удаляет мероприятие: сообщение, ветку, явку и саму запись из базы (безвозвратно)."""
    events = load_json(EVENTS_FILE, {})
    if event_id not in events:
        await interaction.response.send_message(es("❌ Мероприятие не найдено!"), ephemeral=True)
        return
    event = events[event_id]
    if event.get('thread_id'):
        try:
            thread = await client.fetch_channel(event['thread_id'])
            await thread.delete()
        except Exception:
            pass
    try:
        channel = await client.fetch_channel(event['channel_id'])
        message = await channel.fetch_message(event['message_id'])
        await message.delete()
    except Exception:
        pass
    attendance = load_json(ATTENDANCE_FILE, {})
    if event_id in attendance:
        del attendance[event_id]
        save_json(ATTENDANCE_FILE, attendance)
    del events[event_id]
    save_json(EVENTS_FILE, events)
    await interaction.response.send_message(es("🗑️ Мероприятие полностью удалено!"), ephemeral=True)

_STATUS_TITLE_PREFIXES = ("Завершено. ", "Отменено. ")
WEEKDAY_NAMES_BY_INDEX = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]


def get_weekday_name(start_time_epoch: int) -> str:
    """День недели мероприятия, вычисленный по его дате начала (МСК)."""
    dt = datetime.fromtimestamp(start_time_epoch, MSK)
    return WEEKDAY_NAMES_BY_INDEX[dt.weekday()]


def clean_event_title(title: str) -> str:
    """Убирает из НАЧАЛА названия все динамически добавляемые префиксы —
    и статус ('Завершено. ', 'Отменено. '), и день недели ('Суббота. ' и т.д.) —
    если они там случайно оказались (например, из старых данных, где день
    недели или статус были вписаны вручную прямо в название). В базе данных
    хранится ТОЛЬКО чистое название, без этих префиксов — они добавляются
    исключительно при отображении (см. build_display_title)."""
    if not title:
        return title
    prefixes = list(_STATUS_TITLE_PREFIXES) + [f"{day}. " for day in WEEKDAY_NAMES_BY_INDEX]
    changed = True
    while changed:
        changed = False
        for prefix in prefixes:
            if title.startswith(prefix):
                title = title[len(prefix):]
                changed = True
    return title


def build_display_title(event: dict) -> str:
    """Итоговое отображаемое название: '{Статус. }{День недели. }{Чистое название}'.
    День недели присутствует ВСЕГДА (даже при отмене/завершении), статус — только
    когда применим. Используется в embed, названии ветки и списке мероприятий —
    единая точка формирования, чтобы везде было гарантированно одинаково."""
    status = event.get('status', 'active')
    if status == 'cancelled':
        title_prefix = 'Отменено. '
    elif status == 'completed':
        title_prefix = 'Завершено. '
    else:
        title_prefix = ''
    weekday_name = get_weekday_name(event['start_time'])
    return f"{title_prefix}{weekday_name}. {clean_event_title(event['title'])}"

async def build_event_embed(event_id: str) -> discord.Embed:
    events = load_json(EVENTS_FILE, {})
    event = events[event_id]
    current_date = datetime.now(MSK)
    event_start_dt = datetime.fromtimestamp(event['start_time'], MSK)
    active_members = await get_active_members(current_date, registered_before=event_start_dt)
    accepted = list(event.get('accepted', {}).keys())
    declined = list(event.get('declined', {}).keys())
    unmarked = [m for m in active_members if m not in accepted and m not in declined]

    status = event.get('status', 'active')
    embed_color = event.get('color', 15844367)
    if status == 'cancelled':
        embed_color = discord.Color.dark_grey().value
    elif status == 'completed':
        embed_color = discord.Color.greyple().value

    embed = discord.Embed(title=build_display_title(event), description=event['description'], color=embed_color)

    # === ЧИСЛО МАТЧЕЙ (строка сверху, п.7) ===
    num_games = event.get('num_games', 0)
    if num_games and num_games > 0:
        games_word = pluralize_games(num_games)
        embed.add_field(name=es("🎮 Плановые матчи"), value=f"Запланировано: {num_games} {games_word}", inline=False)
    else:
        embed.add_field(name=es("🎮 Плановые матчи"), value="Матчи на мероприятии не запланированы", inline=False)

    # === ОБЯЗАТЕЛЬНОСТЬ ОТМЕТОК (п.11) ===
    if event.get('mandatory', True):
        embed.add_field(name=es("📌 Отметки"), value="Обязательны для всех бойцов", inline=False)
    else:
        embed.add_field(name=es("📌 Отметки"), value="Необязательны", inline=False)

    # === ВРЕМЯ ===
    start_ts = int(event['start_time'])
    end_ts = int(event['end_time'])
    event_end = datetime.fromtimestamp(end_ts, MSK)

    time_value = f"Дата: <t:{start_ts}:F> - <t:{end_ts}:t>"
    # Строка "Начнётся" убирается при отмене/завершении (п.1, п.2)
    if status == 'active' and current_date <= event_end:
        time_value += f"\nНачнется: <t:{start_ts}:R>"

    # === ОЖИДАЕМЫЙ КОМАНДИР ОТДЕЛЕНИЯ (очередь Firebase) (п.4) ===
    if status == 'active':
        expected_commander = await get_expected_squad_commander(event, current_date)
        embed.add_field(
            name=es("🪖 Ожидаемый командир отделения"),
            value=expected_commander if expected_commander else "Не определён",
            inline=False
        )

    embed.add_field(name=es("⏰ Время"), value=time_value, inline=False)

    if accepted:
        embed.add_field(name=es(f"✅ Придут ({len(accepted)})"), value=">>> " + "\n".join(accepted), inline=True)
    if declined:
        embed.add_field(name=es(f"❌ Не придут ({len(declined)})"), value=">>> " + "\n".join(declined), inline=True)
    if unmarked:
        embed.add_field(name=es(f"❓ Не отметились ({len(unmarked)})"), value=">>> " + "\n".join(unmarked), inline=False)

    image_key = event.get('image_key', 'none')
    if image_key != 'none' and image_key in EVENT_IMAGES:
        filename = EVENT_IMAGES[image_key]['file']
        embed.set_image(url=f'attachment://{filename}')

    return embed

async def get_or_create_thread(event, event_id, title):
    if event.get('thread_id'):
        try:
            return await client.fetch_channel(event['thread_id'])
        except Exception:
            pass
    try:
        channel = await client.fetch_channel(event['channel_id'])
        message = await channel.fetch_message(event['message_id'])
        thread = await message.create_thread(name=desired_thread_name(event))
        events = load_json(EVENTS_FILE, {})
        if event_id in events:
            events[event_id]['thread_id'] = thread.id
            save_json(EVENTS_FILE, events)
        return thread
    except Exception:
        return None


async def create_event(title, description, start_time, end_time, image_key='none', num_games=0, color=15844367, mandatory=True):
    event_id = str(uuid.uuid4())
    events = load_json(EVENTS_FILE, {})
    events[event_id] = {
        'title': title, 'description': description,
        'start_time': int(start_time.timestamp()), 'end_time': int(end_time.timestamp()),
        'accepted': {}, 'declined': {},
        'image_key': image_key, 'num_games': num_games, 'color': color,
        'channel_id': EVENTS_CHANNEL_ID, 'message_id': None, 'thread_id': None,
        'reminder_2days_sent': False, 'reminder_15min_sent': False,
        'status': 'active',
        'mandatory': mandatory,
        'created_at': int(datetime.now(MSK).timestamp())
    }
    save_json(EVENTS_FILE, events)
    try:
        channel = await client.fetch_channel(EVENTS_CHANNEL_ID)
        guild = channel.guild
        embed = await build_event_embed(event_id)
        view = build_event_view(events[event_id], event_id)
        filename, path = get_image_info(image_key)
        if filename and path:
            message = await channel.send(embed=embed, view=view, file=discord.File(path, filename=filename))
        else:
            message = await channel.send(embed=embed, view=view)
        events[event_id]['message_id'] = message.id
        thread = await message.create_thread(name=desired_thread_name(events[event_id]))
        events[event_id]['thread_id'] = thread.id
        mention_block = await get_all_active_members_mentions(datetime.now(MSK))
        announcement_msg = await thread.send(render_announcement_message(mention_block))
        record_thread_message(events[event_id], announcement_msg.id, 'announcement', mention_block=mention_block)
        save_json(EVENTS_FILE, events)
    except Exception as e:
        print(f"❌ Ошибка публикации мероприятия: {e}")

async def show_event_list(interaction):
    events = load_json(EVENTS_FILE, {})
    if not events:
        await interaction.response.send_message(es("📭 Нет активных мероприятий"), ephemeral=True)
        return
    text = es("📋 **Активные мероприятия:**\n\n")
    for event_id, event in events.items():
        start = datetime.fromtimestamp(event['start_time'], MSK)
        text += f"**{build_display_title(event)}**\nID: `{event_id}`\n"
        text += f"Дата: {start.strftime('%d.%m.%Y %H:%M')}\n"
        num_games = event.get('num_games', 0)
        if num_games and num_games > 0:
            text += es(f"🎮 Матчей: {num_games}\n")
        text += es(f"✅ Придут: {len(event.get('accepted', {}))}\n")
        text += es(f"❌ Не придут: {len(event.get('declined', {}))}\n\n")
    await interaction.response.send_message(text, ephemeral=True)


async def post_weekly_events():
    ensure_weekly_events_file()
    weekly_events = load_json(WEEKLY_EVENTS_FILE, {})
    now = datetime.now(MSK)
    for weekly_id, entry in weekly_events.items():
        try:
            start_h, start_m = map(int, entry['start_time'].split(':'))
            end_h, end_m = map(int, entry['end_time'].split(':'))
            event_start = get_next_weekday_datetime(entry['day_of_week'], start_h, start_m, from_time=now)
            event_end = event_start.replace(hour=end_h, minute=end_m)
            if event_end <= event_start:
                event_end += timedelta(days=1)
            await create_event(
                clean_event_title(entry['name']), entry['description'], event_start, event_end,
                image_key=entry.get('image_key', 'none'),
                num_games=entry.get('num_games', 0),
                mandatory=entry.get('mandatory', True)
            )
            await asyncio.sleep(2)
        except Exception as e:
            print(f"❌ Ошибка публикации еженедельного мероприятия '{entry.get('name', '?')}': {e}")

async def check_event_reminders():
    events = load_json(EVENTS_FILE, {})
    current_time = datetime.now(MSK)
    changed = False
    for event_id, event in events.items():
        if event.get('status', 'active') != 'active':
            continue
        try:
            event_start = datetime.fromtimestamp(event['start_time'], MSK)
            start_ts = int(event_start.timestamp())
            time_until_start = event_start - current_time

            # === Напоминание за 2 суток — только для ОБЯЗАТЕЛЬНЫХ мероприятий (п.11) ===
            if event.get('mandatory', True) and not event.get('reminder_2days_sent', False):
                if timedelta(hours=47) <= time_until_start <= timedelta(hours=48):
                    active_members = await get_active_members(current_time)
                    accepted = list(event.get('accepted', {}).keys())
                    declined = list(event.get('declined', {}).keys())
                    unmarked = [m for m in active_members if m not in accepted and m not in declined]
                    if unmarked:
                        thread = await get_or_create_thread(event, event_id, event['title'])
                        if thread:
                            mention_block = await build_mentions_for_nicknames(unmarked)
                            msg = await thread.send(render_reminder_2days_message(mention_block))
                            record_thread_message(event, msg.id, 'reminder_2days', mention_block=mention_block)
                    event['reminder_2days_sent'] = True
                    changed = True

            # === Напоминание за 15 минут (п.12, п.15) ===
            if not event.get('reminder_15min_sent', False):
                if timedelta(0) <= time_until_start <= timedelta(minutes=15):
                    thread = await get_or_create_thread(event, event_id, event['title'])
                    should_send = True
                    if thread:
                        if event_created_late(event):
                            # Мероприятие создано менее чем за сутки — тегаем ТОЛЬКО тех,
                            # кто ещё не отметился (не Приду / не Не приду), и кто не в отпуске.
                            # Роль 'Боец ArmA' целиком больше нигде не пингуется.
                            active_members = await get_active_members(current_time)
                            accepted_keys = set(event.get('accepted', {}).keys())
                            declined_keys = set(event.get('declined', {}).keys())
                            unmarked = [m for m in active_members if m not in accepted_keys and m not in declined_keys]
                            mention_block = await build_mentions_for_nicknames(unmarked)
                            should_send = bool(unmarked)
                        else:
                            accepted = list(event.get('accepted', {}).keys())
                            mention_block = await build_mentions_for_nicknames(accepted)
                            should_send = bool(accepted)
                        if should_send:
                            msg = await thread.send(render_reminder_15min_message(mention_block, event))
                            record_thread_message(event, msg.id, 'reminder_15min', mention_block=mention_block)
                    event['reminder_15min_sent'] = True
                    changed = True
        except Exception:
            pass
    if changed:
        save_json(EVENTS_FILE, events)

async def check_event_completion():
    """Автоматически переводит активные мероприятия в статус 'completed' после окончания."""
    events = load_json(EVENTS_FILE, {})
    current_time = datetime.now(MSK)
    changed = False
    completed_ids = []
    for event_id, event in events.items():
        if event.get('status', 'active') != 'active':
            continue
        try:
            event_end = datetime.fromtimestamp(event['end_time'], MSK)
        except Exception:
            continue
        if current_time > event_end:
            event['status'] = 'completed'
            changed = True
            completed_ids.append(event_id)
    if changed:
        save_json(EVENTS_FILE, events)
        for event_id in completed_ids:
            event = events[event_id]
            await refresh_event_message(event_id)
            if event.get('thread_id'):
                try:
                    thread = await client.fetch_channel(event['thread_id'])
                    await thread.edit(name=desired_thread_name(event))
                    msg = await thread.send(render_completion_message(event))
                    record_thread_message(event, msg.id, 'completion')
                    await lock_and_archive_thread(thread)
                except Exception:
                    pass
            await process_inactivity_discipline_for_event(event_id)
        save_json(EVENTS_FILE, events)


# ============== ОБНОВЛЕНИЕ ШАБЛОНОВ СООБЩЕНИЙ ==============

async def update_all_templates():
    """Обновляет шаблоны всех сообщений бота:
    - все сообщения мероприятий
    - все сообщения отпусков (и активные, и завершённые, и отклонённые)
    - правила отпусков в канале отпусков
    - оформление трёх якорных сообщений админ-канала
    """
    anchors_fixed = 0
    try:
        anchors = load_json(ADMIN_ANCHORS_FILE, {})
        admin_channel = await client.fetch_channel(ADMIN_CHANNEL_ID)

        if anchors.get('panel_message_id'):
            try:
                msg = await admin_channel.fetch_message(anchors['panel_message_id'])
                await msg.edit(embed=build_admin_panel_embed(), view=AdminMainMenuView())
                anchors_fixed += 1
            except Exception as e:
                print(f"⚠️ Не удалось обновить якорь 'Панель управления': {e}")

        if anchors.get('notifications_message_id'):
            try:
                msg = await admin_channel.fetch_message(anchors['notifications_message_id'])
                await msg.edit(embed=build_notifications_anchor_embed())
                anchors_fixed += 1
            except Exception as e:
                print(f"⚠️ Не удалось обновить якорь 'Уведомления': {e}")

        if anchors.get('logging_message_id'):
            try:
                msg = await admin_channel.fetch_message(anchors['logging_message_id'])
                await msg.edit(embed=build_logging_anchor_embed())
                anchors_fixed += 1
            except Exception as e:
                print(f"⚠️ Не удалось обновить якорь 'Логирование': {e}")
    except Exception as e:
        print(f"❌ Ошибка синхронизации якорных сообщений: {e}")

    events = load_json(EVENTS_FILE, {})
    vacations = load_json(VACATIONS_FILE, {})
    
    ev_updated = 0
    ev_errors = 0
    vac_updated = 0
    vac_errors = 0
    
    # === 1. ОБНОВЛЕНИЕ СООБЩЕНИЙ МЕРОПРИЯТИЙ ===
    for event_id, event in events.items():
        if 'status' not in event:
            event['status'] = 'active'
        if 'mandatory' not in event:
            event['mandatory'] = True
        if 'created_at' not in event:
            # считаем, что старые мероприятия создавались заранее (не «поздно»)
            event['created_at'] = event.get('start_time', int(datetime.now(MSK).timestamp())) - 7 * 24 * 3600
        cleaned_title = clean_event_title(event.get('title', ''))
        if cleaned_title != event.get('title', ''):
            event['title'] = cleaned_title
        title = event.get('title', '').lower()

        original_image = event.get('image_key', 'none')
        original_games = event.get('num_games', 0)
        
        image_key = original_image
        if image_key == 'none' or image_key not in EVENT_IMAGES:
            if 'as vdv' in title or ('rtvt' in title and 'tt' not in title):
                image_key = 'asvdv'
            elif (' tt' in title or title.endswith('tt') or 'triad' in title) and 'tvt' in title:
                image_key = 'tt'
            elif 'echo' in title:
                image_key = 'echo'
            elif 'межклан' in title:
                image_key = 'mezhklan'
            elif 'внутриклан' in title:
                image_key = 'vnutriklan'
            elif 'вылазка' in title:
                image_key = 'vylazka'
            elif 'мангуст' in title:
                image_key = 'mangust'
        
        num_games = original_games if original_games and original_games > 0 else 0
        if num_games == 0:
            if image_key == 'asvdv':
                num_games = 2
            elif image_key == 'tt':
                num_games = 3
        
        event['image_key'] = image_key
        event['num_games'] = num_games
        
        try:
            channel = await client.fetch_channel(event['channel_id'])
            message = await channel.fetch_message(event['message_id'])
            embed = await build_event_embed(event_id)
            filename, path = get_image_info(image_key)
            if filename and path:
                await message.edit(embed=embed, attachments=[discord.File(path, filename=filename)], view=build_event_view(event, event_id))
            else:
                await message.edit(embed=embed, attachments=[], view=build_event_view(event, event_id))
            ev_updated += 1
            await asyncio.sleep(4)  # Защита от rate limit
        except discord.NotFound:
            ev_errors += 1
        except Exception as e:
            print(f"❌ Ошибка обновления мероприятия '{event.get('title', '?')}': {e}")
            ev_errors += 1
    
    save_json(EVENTS_FILE, events)

    # === 1.5. РЕСИНХРОНИЗАЦИЯ НАЗВАНИЙ ВЕТОК, ВСЕХ СООБЩЕНИЙ БОТА В НИХ, БЛОКИРОВКИ (п.3, п.4) ===
    thread_names_fixed = 0
    thread_messages_fixed = 0
    thread_locks_fixed = 0
    attendance_for_sync = load_json(ATTENDANCE_FILE, {})
    for event_id, event in events.items():
        thread_id = event.get('thread_id')
        if not thread_id:
            continue
        try:
            thread = await client.fetch_channel(thread_id)
        except Exception:
            continue

        desired_name = desired_thread_name(event)
        if thread.name != desired_name:
            try:
                was_locked = getattr(thread, 'locked', False)
                if was_locked:
                    await unlock_and_unarchive_thread(thread)
                await thread.edit(name=desired_name)
                if was_locked:
                    await lock_and_archive_thread(thread)
                thread_names_fixed += 1
            except Exception as e:
                print(f"⚠️ Не удалось обновить название ветки {thread_id}: {e}")

        for msg_record in event.get('thread_messages', []):
            try:
                msg = await thread.fetch_message(msg_record['id'])
            except Exception:
                continue
            kind = msg_record.get('kind')
            extra = msg_record.get('extra', {})
            mention_block = msg_record.get('mention_block', '')
            new_text = None
            if kind == 'announcement':
                new_text = render_announcement_message(mention_block)
            elif kind == 'completion':
                new_text = render_completion_message(event)
            elif kind == 'early_completion':
                new_text = render_early_completion_message(event)
            elif kind == 'cancelled':
                new_text = render_cancel_message(event, extra.get('by_user', '?'))
            elif kind == 'reactivated':
                new_text = render_reactivate_message(event)
            elif kind == 'reminder_2days':
                new_text = render_reminder_2days_message(mention_block)
            elif kind == 'reminder_15min':
                new_text = render_reminder_15min_message(mention_block, event)
            elif kind == 'mods':
                new_text = render_mods_message(mention_block, event, extra.get('server_name'), extra.get('password'))
            if new_text is not None and msg.content != new_text:
                try:
                    await msg.edit(content=new_text)
                    thread_messages_fixed += 1
                    await asyncio.sleep(1)
                except Exception as e:
                    print(f"⚠️ Не удалось обновить сообщение {msg_record.get('id')} в ветке {thread_id}: {e}")

        if event.get('status') == 'completed' and event_id in attendance_for_sync:
            if not getattr(thread, 'locked', False) or not getattr(thread, 'archived', False):
                await lock_and_archive_thread(thread)
                thread_locks_fixed += 1

    # === 2. ОБНОВЛЕНИЕ СООБЩЕНИЙ ОТПУСКОВ ===
    for nickname, data in vacations.items():
        if not data.get('message_id') or not data.get('channel_id'):
            continue
        
        try:
            channel = await client.fetch_channel(data['channel_id'])
            message = await channel.fetch_message(data['message_id'])
            
            if not message.embeds:
                continue
            
            embed = message.embeds[0]
            status = data.get('status', 'pending')
            by_admin = data.get('by_admin', False)
            created_by = data.get('created_by', 'Сам боец' if not by_admin else 'Неизвестно')
            
            # === ОБНОВЛЯЕМ ПОЛЕ "📅 Период" с метками времени Discord ===
            for i, field in enumerate(embed.fields):
                if field.name == es("📅 Период"):
                    new_period = format_vacation_period(data['start'], data['end'])
                    embed.set_field_at(i, name=es("📅 Период"), value=new_period, inline=False)
                    break
            
            # === ОБНОВЛЯЕМ ПОЛЕ "Оформил/Запросил" в зависимости от by_admin ===
            if by_admin:
                requester_field_name = es("👤 Оформил")
                requester_field_value = f"Комбат или заместитель: {created_by}"
            else:
                requester_field_name = es("👤 Запросил")
                requester_field_value = created_by
            
            field_found = False
            for i, field in enumerate(embed.fields):
                if field.name in [es("👤 Оформил"), es("👤 Запросил")]:
                    embed.set_field_at(i, name=requester_field_name, value=requester_field_value, inline=False)
                    field_found = True
                    break
            
            if not field_found:
                embed.add_field(name=requester_field_name, value=requester_field_value, inline=False)
            
            # === ОБНОВЛЯЕМ TITLE, DESCRIPTION, COLOR В ЗАВИСИМОСТИ ОТ СТАТУСА ===
            if status == 'pending':
                embed.title = es("🏖️ Отпуск требует утверждения")
                embed.description = f"Отпуск для **{nickname}**" if by_admin else f"**{nickname}** запросил(а) отпуск"
                embed.color = discord.Color.orange()
                for i, field in enumerate(embed.fields):
                    if field.name == es("ℹ️ Статус"):
                        embed.set_field_at(i, name=es("ℹ️ Статус"), value="Ожидает утверждения комбатом", inline=False)
                await message.edit(embed=embed, view=VacationApprovalView())
                
            elif status == 'active':
                embed.title = es("🏖️ Отпуск утверждён")
                embed.description = f"Отпуск для **{nickname}**" if by_admin else f"**{nickname}** взял отпуск"
                embed.color = discord.Color.green()
                for i, field in enumerate(embed.fields):
                    if field.name == es("ℹ️ Статус"):
                        embed.set_field_at(i, name=es("ℹ️ Статус"), value="Утверждён и активен", inline=False)
                await message.edit(embed=embed, view=VacationMessageView())
                
            elif status in ['rejected', 'ended_early', 'ended_scheduled']:
                if status == 'rejected':
                    embed.title = es("❌ Отпуск отклонён")
                    embed.description = f"Отпуск для **{nickname}** отклонён" if by_admin else f"Запрос на отпуск **{nickname}** отклонён"
                    embed.color = discord.Color.red()
                    status_text = "Отклонён командованием"
                elif status == 'ended_early':
                    embed.title = es("🏖️ Отпуск утверждён")
                    embed.description = f"Отпуск для **{nickname}**" if by_admin else f"**{nickname}** взял отпуск"
                    embed.color = discord.Color.red()
                    status_text = "Завершен досрочно"
                else:  # ended_scheduled
                    embed.title = es("🏖️ Отпуск утверждён")
                    embed.description = f"Отпуск для **{nickname}**" if by_admin else f"**{nickname}** взял отпуск"
                    embed.color = discord.Color.greyple()
                    status_text = "Завершен по истечению срока"
                
                for i, field in enumerate(embed.fields):
                    if field.name == es("ℹ️ Статус"):
                        embed.set_field_at(i, name=es("ℹ️ Статус"), value=status_text, inline=False)
                
                await message.edit(embed=embed, view=None)
            
            vac_updated += 1
            await asyncio.sleep(4)  # Защита от rate limit
        except discord.NotFound:
            vac_errors += 1
        except Exception as e:
            print(f"❌ Ошибка обновления отпуска '{nickname}': {e}")
            vac_errors += 1

    
    # === 3. ОБНОВЛЕНИЕ ПРАВИЛ ОТПУСКОВ ===
    try:
        channel = await client.fetch_channel(VACATION_CHANNEL_ID)
        rules_updated = False
        async for message in channel.history(limit=20):
            if message.author.id != client.user.id:
                continue
            if message.embeds and message.embeds[0].title == es("🏖️ Оформление отпусков"):
                embed = discord.Embed(title=es("🏖️ Оформление отпусков"), description=VACATION_RULES, color=discord.Color.green())
                embed.set_footer(text="Нажмите кнопку ниже, чтобы оформить отпуск")
                await message.edit(embed=embed, view=VacationRequestView())
                rules_updated = True
                vac_updated += 1
                break
        if not rules_updated:
            embed = discord.Embed(title=es("🏖️ Оформление отпусков"), description=VACATION_RULES, color=discord.Color.green())
            embed.set_footer(text="Нажмите кнопку ниже, чтобы оформить отпуск")
            await channel.send(embed=embed, view=VacationRequestView())
            vac_updated += 1
    except Exception as e:
        print(f"❌ Ошибка обновления правил отпусков: {e}")
        vac_errors += 1
    
    # === 4. ОБНОВЛЕНИЕ ОТЧЁТОВ О ЯВКЕ В ВЕТКАХ МЕРОПРИЯТИЙ ===
    attendance = load_json(ATTENDANCE_FILE, {})
    att_updated = 0
    att_errors = 0
    for event_id, record in attendance.items():
        if not record.get('attendance_message_id') or not record.get('thread_id'):
            continue
        thread = None
        was_locked = False
        try:
            thread = await client.fetch_channel(record['thread_id'])
            # Большинство мероприятий на этот момент уже завершены — их ветки
            # заблокированы/заархивированы (см. П.1/П.2), а Discord отклоняет
            # ЛЮБУЮ правку содержимого в такой ветке (50083: Thread is archived).
            # Поэтому временно открываем ветку перед редактированием.
            was_locked = getattr(thread, 'locked', False) or getattr(thread, 'archived', False)
            if was_locked:
                await unlock_and_unarchive_thread(thread)

            message = await thread.fetch_message(record['attendance_message_id'])
            await message.edit(content=build_attendance_report_text(record))
            att_updated += 1
            await asyncio.sleep(2)
        except discord.NotFound:
            att_errors += 1
        except Exception as e:
            print(f"❌ Ошибка обновления отчёта явки '{record.get('title','?')}': {e}")
            att_errors += 1
        finally:
            # Возвращаем ветку в закрытое состояние, если она была такой ДО правки
            # (то есть если мероприятие завершено и должно оставаться заблокированным).
            if thread and was_locked:
                await lock_and_archive_thread(thread)

    print(f"🔄 Итог обновления шаблонов:")
    print(f"   📅 Мероприятий: обновлено {ev_updated}, ошибок {ev_errors}")
    print(f"   🏖️ Отпусков: обновлено {vac_updated}, ошибок {vac_errors}")
    print(f"   🏆 Отчётов о явке: обновлено {att_updated}, ошибок {att_errors}")
    print(f"   💬 Названий веток исправлено: {thread_names_fixed}, сообщений в ветках обновлено: {thread_messages_fixed}, блокировок исправлено: {thread_locks_fixed}")
    print(f"   🛠️ Якорных сообщений обновлено: {anchors_fixed}")
    
    return ev_updated, ev_errors, vac_updated, vac_errors, att_updated, att_errors, thread_names_fixed, thread_messages_fixed, thread_locks_fixed, anchors_fixed


# ============== ПОСТОЯННАЯ ФУНКЦИЯ ИЗВЛЕЧЕНИЯ ==============

async def extract_message_structure(interaction, channel_id, message_id):
    try:
        channel = await client.fetch_channel(channel_id)
        message = await channel.fetch_message(message_id)
        result = []
        result.append(f"=== СООБЩЕНИЕ ID: {message.id} ===")
        result.append(f"Автор: {message.author}")
        result.append(f"Content: {message.content}")
        result.append("")
        if message.embeds:
            result.append(f"=== EMBEDS ({len(message.embeds)}) ===")
            for i, embed in enumerate(message.embeds):
                result.append(f"--- Embed {i+1} ---")
                result.append(f"Title: {embed.title}")
                result.append(f"Description: {embed.description}")
                result.append(f"Color: {embed.color}")
                result.append("")
                if embed.image:
                    result.append(f"IMAGE: url={embed.image.url}")
                if embed.thumbnail:
                    result.append(f"THUMBNAIL: url={embed.thumbnail.url}")
                result.append(f"Fields ({len(embed.fields)}):")
                for j, field in enumerate(embed.fields):
                    result.append(f"  Field {j+1}: name='{field.name}', value='{field.value}', inline={field.inline}")
                if embed.footer:
                    result.append(f"Footer: {embed.footer.text}")
                result.append("")
                result.append("RAW DICT:")
                result.append(json.dumps(embed.to_dict(), indent=2, ensure_ascii=False))
        else:
            result.append("Нет embeds")
        result.append("")
        result.append(f"=== ATTACHMENTS ({len(message.attachments)}) ===")
        for att in message.attachments:
            result.append(f"Attachment: filename={att.filename}, url={att.url}")
        result.append("")
        result.append(f"=== COMPONENTS ({len(message.components)}) ===")
        for i, row in enumerate(message.components):
            result.append(f"Row {i+1}:")
            for comp in row.children:
                result.append(f"  {comp.type}: label={getattr(comp, 'label', None)}, custom_id={getattr(comp, 'custom_id', None)}")
        output = "\n".join(result)
        print(output)
        if len(output) <= 1900:
            await interaction.response.send_message(f"```\n{output}\n```", ephemeral=True)
        else:
            parts = [output[i:i+1900] for i in range(0, len(output), 1900)]
            await interaction.response.send_message(f"```\n{parts[0]}\n```", ephemeral=True)
            for part in parts[1:]:
                await interaction.followup.send(f"```\n{part}\n```", ephemeral=True)
        return output
    except Exception as e:
        await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)
        return None


# ============== ВРЕМЕННЫЕ ГОЛОСОВЫЕ КОМНАТЫ ==============

async def setup_voice_room_triggers(guild):
    global TRIGGER_CHANNEL_ARMY, TRIGGER_CHANNEL_PUBLIC
    
    try:
        category = guild.get_channel(VOICE_ROOM_CATEGORY_ARMY)
        if not category:
            print(f"⚠️ Категория {VOICE_ROOM_CATEGORY_ARMY} не найдена")
        else:
            existing = None
            for ch in category.voice_channels:
                if ch.name == "🔵 Создать голосовую комнату":
                    existing = ch
                    break
            
            if existing:
                TRIGGER_CHANNEL_ARMY = existing.id
                print(f"✅ Найден существующий 🔵 триггер-канал: {existing.id}")
            else:
                ref_channel = guild.get_channel(VOICE_CHANNEL_ID)
                overwrites = ref_channel.overwrites if ref_channel else {}
                
                new_channel = await guild.create_voice_channel(
                    name="🔵 Создать голосовую комнату",
                    category=category,
                    overwrites=overwrites,
                    user_limit=0
                )
                TRIGGER_CHANNEL_ARMY = new_channel.id
                print(f"✅ Создан 🔵 триггер-канал: {new_channel.id}")
    except Exception as e:
        print(f"❌ Ошибка создания 🔵 триггер-канала: {e}")
    
    try:
        category = guild.get_channel(VOICE_ROOM_CATEGORY_PUBLIC)
        if not category:
            print(f"⚠️ Категория {VOICE_ROOM_CATEGORY_PUBLIC} не найдена")
        else:
            existing = None
            for ch in category.voice_channels:
                if ch.name == "🍻 Создать голосовую комнату":
                    existing = ch
                    break
            
            if existing:
                TRIGGER_CHANNEL_PUBLIC = existing.id
                print(f"✅ Найден существующий 🍻 триггер-канал: {existing.id}")
            else:
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(
                        view_channel=True,
                        connect=True,
                        speak=True,
                        stream=True,
                        use_voice_activation=True
                    )
                }
                
                new_channel = await guild.create_voice_channel(
                    name="🍻 Создать голосовую комнату",
                    category=category,
                    overwrites=overwrites,
                    user_limit=0
                )
                TRIGGER_CHANNEL_PUBLIC = new_channel.id
                print(f"✅ Создан 🍻 триггер-канал: {new_channel.id}")
    except Exception as e:
        print(f"❌ Ошибка создания 🍻 триггер-канала: {e}")


async def create_temp_voice_room(member, trigger_channel):
    global VOICE_ROOMS
    
    guild = member.guild
    category = trigger_channel.category
    
    is_public = (trigger_channel.id == TRIGGER_CHANNEL_PUBLIC)
    
    if is_public:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=True, connect=True, speak=True,
                stream=True, use_voice_activation=True
            ),
            member: discord.PermissionOverwrite(
                view_channel=True, connect=True, speak=True,
                manage_channels=True, manage_roles=True,
                mute_members=True, deafen_members=True,
                move_members=True
            )
        }
    else:
        ref_channel = guild.get_channel(VOICE_CHANNEL_ID)
        overwrites = dict(ref_channel.overwrites) if ref_channel else {}
        overwrites[member] = discord.PermissionOverwrite(
            view_channel=True, connect=True, speak=True,
            manage_channels=True, manage_roles=True,
            mute_members=True, deafen_members=True,
            move_members=True
        )
    
    try:
        clean_name = member.display_name
        room_name = f"🚪 Комната {clean_name}"
        if len(room_name) > 100:
            room_name = room_name[:100]
        
        temp_channel = await guild.create_voice_channel(
            name=room_name,
            category=category,
            overwrites=overwrites,
            user_limit=0
        )
        
        VOICE_ROOMS[temp_channel.id] = {
            'owner_id': member.id,
            'created_at': datetime.now(MSK).isoformat(),
            'is_public': is_public
        }
        save_voice_rooms()
        
        await member.move_to(temp_channel, reason="Создание временной голосовой комнаты")
        
        print(f"✅ Создана временная комната '{room_name}' для {member.display_name} (ID: {temp_channel.id})")
        return temp_channel
    except Exception as e:
        print(f"❌ Ошибка создания временной комнаты: {e}")
        return None


async def cleanup_empty_temp_room(channel_id):
    global VOICE_ROOMS
    
    if channel_id not in VOICE_ROOMS:
        return
    
    try:
        channel = client.get_channel(channel_id)
        if channel is None:
            del VOICE_ROOMS[channel_id]
            save_voice_rooms()
            return
        
        if len(channel.members) == 0:
            await channel.delete(reason="Временная комната пуста — автоудаление")
            del VOICE_ROOMS[channel_id]
            save_voice_rooms()
            print(f"🗑️ Удалена пустая временная комната (ID: {channel_id})")
    except discord.NotFound:
        if channel_id in VOICE_ROOMS:
            del VOICE_ROOMS[channel_id]
            save_voice_rooms()
    except Exception as e:
        print(f"⚠️ Ошибка при удалении временной комнаты: {e}")

        
TEMP_ROOM_NAME_PREFIX = "🚪 Комната "


def save_voice_rooms():
    """Сохраняет текущее состояние временных комнат на диск (переживает рестарт бота)."""
    save_json(VOICE_ROOMS_FILE, {str(k): v for k, v in VOICE_ROOMS.items()})


def load_voice_rooms():
    """Загружает состояние временных комнат с диска в память при старте бота."""
    global VOICE_ROOMS
    data = load_json(VOICE_ROOMS_FILE, {})
    VOICE_ROOMS = {int(k): v for k, v in data.items()}


async def sync_voice_rooms_on_startup(guild):
    """Сверяет состояние временных комнат с реальностью при старте бота.
    Закрывает эффект 'зависшей' комнаты, которая осталась пустой, пока бот был offline
    (например, рестарт бота произошёл в момент, когда все вышли из комнаты, и
    событие on_voice_state_update было пропущено из-за разрыва gateway-сессии)."""
    load_voice_rooms()

    # 1. Чистим то, что бот УЖЕ знал (из старой сессии) — вдруг оно уже пустое или удалено
    ids_to_remove = []
    for channel_id in list(VOICE_ROOMS.keys()):
        channel = guild.get_channel(channel_id)
        if channel is None:
            ids_to_remove.append(channel_id)
            continue
        if len(channel.members) == 0:
            try:
                await channel.delete(reason="Временная комната пуста — очистка при запуске бота")
                print(f"🗑️ [Синхронизация] Удалена зависшая пустая комната (ID: {channel_id})")
            except Exception as e:
                print(f"⚠️ [Синхронизация] Не удалось удалить зависшую комнату {channel_id}: {e}")
            ids_to_remove.append(channel_id)

    for channel_id in ids_to_remove:
        VOICE_ROOMS.pop(channel_id, None)

    # 2. Сканируем сами категории на предмет "бесхозных" комнат,
    #    о которых бот вообще не знает (например, если voice_rooms.json ещё не существовал)
    for category_id in (VOICE_ROOM_CATEGORY_ARMY, VOICE_ROOM_CATEGORY_PUBLIC):
        category = guild.get_channel(category_id)
        if not category:
            continue
        for ch in category.voice_channels:
            if ch.id in (TRIGGER_CHANNEL_ARMY, TRIGGER_CHANNEL_PUBLIC):
                continue
            if not ch.name.startswith(TEMP_ROOM_NAME_PREFIX):
                continue
            if ch.id in VOICE_ROOMS:
                continue

            if len(ch.members) == 0:
                try:
                    await ch.delete(reason="Обнаружена бесхозная пустая временная комната — очистка при запуске бота")
                    print(f"🗑️ [Синхронизация] Удалена бесхозная пустая комната '{ch.name}' (ID: {ch.id})")
                except Exception as e:
                    print(f"⚠️ [Синхронизация] Не удалось удалить бесхозную комнату {ch.id}: {e}")
            else:
                owner_id = None
                for target, overwrite in ch.overwrites.items():
                    if isinstance(target, discord.Member) and overwrite.manage_channels:
                        owner_id = target.id
                        break
                if owner_id is None and ch.members:
                    owner_id = ch.members[0].id
                VOICE_ROOMS[ch.id] = {
                    'owner_id': owner_id,
                    'created_at': datetime.now(MSK).isoformat(),
                    'is_public': (category_id == VOICE_ROOM_CATEGORY_PUBLIC)
                }
                print(f"♻️ [Синхронизация] Восстановлено отслеживание непустой комнаты '{ch.name}' (ID: {ch.id})")

    save_voice_rooms()


async def sweep_empty_voice_rooms():
    """Периодическая защитная проверка на случай пропущенных событий on_voice_state_update
    (например, при разрыве и переподключении gateway-сессии). Работает как страховка
    поверх обычной логики удаления при выходе из комнаты."""
    for channel_id in list(VOICE_ROOMS.keys()):
        await cleanup_empty_temp_room(channel_id)

async def force_restart_bot():
    """Освобождает lock-файл, поднимает НОВЫЙ независимый процесс бота
    (вывод которого продолжает писаться в тот же logs/bot_output.log)
    и завершает текущий процесс. Вызывается кнопкой '🔄 Принудительный
    перезапуск' в админ-панели."""
    try:
        await flush_log_buffer_to_discord()
    except Exception:
        pass

    try:
        _release_instance_lock()
    except Exception as e:
        _original_print(f"⚠️ Ошибка при освобождении lock-файла перед перезапуском: {e}")

    try:
        log_dir = os.path.join(BASE_DIR, 'logs')
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, 'bot_output.log')
        log_file = open(log_path, 'a', encoding='utf-8')
        kwargs = {}
        if os.name == 'nt':
            kwargs['creationflags'] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs['start_new_session'] = True
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__)],
            cwd=BASE_DIR, stdout=log_file, stderr=log_file, stdin=subprocess.DEVNULL,
            **kwargs
        )
        _original_print("🔄 Новый процесс бота запущен, завершаю текущий...")
    except Exception as e:
        _original_print(f"❌ Не удалось запустить новый процесс бота при перезапуске: {e}")

    os._exit(0)


# ============== СОБЫТИЯ DISCORD ==============

@client.event
async def on_voice_state_update(member, before, after):
    """Обработчик для временных голосовых комнат с защитой от двойного создания."""
    global TRIGGER_CHANNEL_ARMY, TRIGGER_CHANNEL_PUBLIC, VOICE_ROOMS, VOICE_ROOM_CREATION_LOCKS
    
    if member.bot:
        return
    
    # === СЛУЧАЙ 1: Пользователь подключился к триггер-каналу ===
    if after.channel and after.channel.id in [TRIGGER_CHANNEL_ARMY, TRIGGER_CHANNEL_PUBLIC]:
        # Получаем или создаём lock для этого пользователя (защита от race condition)
        if member.id not in VOICE_ROOM_CREATION_LOCKS:
            VOICE_ROOM_CREATION_LOCKS[member.id] = asyncio.Lock()
        
        async with VOICE_ROOM_CREATION_LOCKS[member.id]:
            # Проверяем, не создана ли уже комната для этого пользователя
            for room_id, room_data in VOICE_ROOMS.items():
                if room_data['owner_id'] == member.id:
                    return
            
            # Создаём временную комнату
            await create_temp_voice_room(member, after.channel)
        return
    
    # === СЛУЧАЙ 2: Пользователь вышел из временной комнаты ===
    if before.channel and before.channel.id in VOICE_ROOMS:
        await asyncio.sleep(2)
        await cleanup_empty_temp_room(before.channel.id)


@client.event
async def on_ready():
    print(f'Бот запущен как {client.user} (PID {os.getpid()})')
    
    await load_all_firebase_data()
    ensure_weekly_events_file()
    await load_clan_members_from_firebase()
    
    for guild in client.guilds:
        await setup_voice_room_triggers(guild)
        await sync_voice_rooms_on_startup(guild)
    
    if not scheduler.get_job('spreadsheet_check'):
        scheduler.add_job(
            scheduled_check_spreadsheet, 'cron', day='*/2', hour=18, minute=0,
            id='spreadsheet_check', replace_existing=True,
            max_instances=1, coalesce=True, misfire_grace_time=1800
        )
    if not scheduler.get_job('weekly_events'):
        scheduler.add_job(post_weekly_events, 'cron', day_of_week='mon', hour=12, minute=0, id='weekly_events', replace_existing=True)
    if not scheduler.get_job('vacation_check'):
        scheduler.add_job(check_expired_vacations, 'interval', hours=1, id='vacation_check', replace_existing=True)
    if not scheduler.get_job('vacation_ending_reminder'):
        scheduler.add_job(check_vacation_ending_soon, 'interval', hours=1, id='vacation_ending_reminder', replace_existing=True)
    if not scheduler.get_job('event_reminders'):
        scheduler.add_job(check_event_reminders, 'interval', minutes=1, id='event_reminders', replace_existing=True)
    if not scheduler.get_job('event_completion_check'):
        scheduler.add_job(check_event_completion, 'interval', minutes=5, id='event_completion_check', replace_existing=True)
    if not scheduler.get_job('clan_cache_refresh'):
        scheduler.add_job(load_clan_members_from_firebase, 'interval', hours=1, id='clan_cache_refresh', replace_existing=True)
    if not scheduler.get_job('voice_rooms_sweep'):
        scheduler.add_job(sweep_empty_voice_rooms, 'interval', minutes=10, id='voice_rooms_sweep', replace_existing=True)
    if not scheduler.get_job('log_forward_flush'):
        scheduler.add_job(flush_log_buffer_to_discord, 'interval', seconds=15, id='log_forward_flush', replace_existing=True)
    if not scheduler.get_job('disciplinary_cleanup'):
        scheduler.add_job(cleanup_expired_disciplinary_actions, 'interval', hours=6, id='disciplinary_cleanup', replace_existing=True)

    if not scheduler.running:
        scheduler.start()

    # Сразу отправляем всё, что накопилось в буфере логов за время запуска бота
    await flush_log_buffer_to_discord()
    
    client.add_view(AdminMainMenuView())
    client.add_view(VacationRequestView())
    client.add_view(VacationApprovalView())
    client.add_view(VacationMessageView())
    register_persistent_event_views()

    await setup_firestore_watchers()

    try:
        await ensure_admin_channel_anchors()
    except Exception as e:
        print(f"⚠️ Не удалось инициализировать якорные сообщения админ-канала: {e}")


@client.event
async def on_message(message):
    if dedup.mark_processed(message.id):
        return
    if message.author == client.user:
        return
    if message.author.id not in ADMIN_USER_IDS:
        return
    if message.content.startswith('!check'):
        if check_lock.locked():
            await message.channel.send(es("⚠️ Проверка уже выполняется."))
            return
        await message.channel.send(es("🔍 Запускаю проверку таблицы..."))
        await check_spreadsheet()
        await message.add_reaction('✅')
        return
    if not message.content.startswith(PREFIX):
        return
    text = message.content[len(PREFIX):].strip()
    if not text:
        return
    try:
        thread = await client.fetch_channel(THREAD_ID)
        await thread.send(text)
        await message.add_reaction('✅')
    except Exception as e:
        await message.channel.send(f"❌ Ошибка: {e}")


if __name__ == '__main__':
    try:
        client.run(os.environ['DISCORD_TOKEN'])
    finally:
        # wait=True — дожидаемся завершения всех фоновых записей в Firebase перед выходом
        EXECUTOR.shutdown(wait=True)
        if os.path.exists(LOCK_FILE):
            try:
                with open(LOCK_FILE, 'r') as f:
                    saved_pid = f.read().strip()
                if saved_pid == str(os.getpid()):
                    os.remove(LOCK_FILE)
            except Exception:
                pass