import discord
import os
import sys
import signal
import atexit
import gspread
import re
import json
import uuid
from datetime import datetime, timedelta
from collections import deque
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import asyncio
from concurrent.futures import ThreadPoolExecutor

# Глобальный executor для синхронных операций (gspread использует requests)
EXECUTOR = ThreadPoolExecutor(max_workers=5)

# ============== ЗАЩИТА ОТ ДВОЙНОГО ЗАПУСКА ==============

LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.bot.lock')


def _pid_is_alive(pid: int) -> bool:
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:
        pass
    if os.path.exists(f"/proc/{pid}"):
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def acquire_single_instance_lock():
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, 'r') as f:
                old_pid_str = f.read().strip()
            old_pid = int(old_pid_str) if old_pid_str.isdigit() else None
        except Exception:
            old_pid = None
        if old_pid and _pid_is_alive(old_pid):
            print(f"❌ Бот уже запущен (PID {old_pid}). Останавливаю этот процесс.")
            sys.exit(1)
        else:
            print(f"⚠️ Найден устаревший lock-файл (PID {old_pid} не активен). Перезаписываю.")
    with open(LOCK_FILE, 'w') as f:
        f.write(str(os.getpid()))
    def _cleanup():
        try:
            if os.path.exists(LOCK_FILE):
                with open(LOCK_FILE, 'r') as f:
                    saved_pid = f.read().strip()
                if saved_pid == str(os.getpid()):
                    os.remove(LOCK_FILE)
        except Exception as e:
            print(f"Ошибка при удалении lock-файла: {e}")
    atexit.register(_cleanup)
    def _signal_handler(signum, frame):
        print(f"Получен сигнал {signum}, завершаю работу...")
        _cleanup()
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
        '🖼️ ': '🖼️ㅤ', '🚫 ': '🚫ㅤ',
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
VOICE_CHANNEL_ID = 1284893513921728582
VOICE_CHANNEL_URL = "https://discord.com/channels/734494109032513699/1284893513921728582"

VOICE_ROOM_CATEGORY_ARMY = 1284893244878098464
VOICE_ROOM_CATEGORY_PUBLIC = 1116657923360301157

EVENTS_FILE = 'events_data.json'
VACATIONS_FILE = 'vacations.json'
ATTENDANCE_FILE = 'attendance_data.json'

MAX_GAMES = 10
MAX_SELECT_OPTIONS = 25

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IMAGES_DIR = os.path.join(BASE_DIR, 'images')

EVENT_IMAGES = {
    'echo': {'file': 'echo-rounded.png', 'title': 'Плановая игра на ECHO'},
    'asvdv': {'file': 'asvdv-rounded.png', 'title': 'Плановая игра на AS VDV'},
    'tt': {'file': 'tt-rounded.png', 'title': 'Плановая игра на Triad Tactics'},
    'mezhklan': {'file': 'mezhklan-rounded.png', 'title': 'Межклановое мероприятие'},
    'vnutriklan': {'file': 'vnutriklan-rounded.png', 'title': 'Внутриклановое мероприятие'},
    'vylazka': {'file': 'vylazka-rounded.png', 'title': 'Клановая вылазка'},
    'mangust': {'file': 'mangust-rounded.png', 'title': 'Операция «Мангуст»'},
}


def get_image_path(image_key: str):
    if image_key == 'none' or image_key not in EVENT_IMAGES:
        return None
    path = os.path.join(IMAGES_DIR, EVENT_IMAGES[image_key]['file'])
    return path if os.path.exists(path) else None


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
        return "игр"
    last_digit = num % 10
    last_two_digits = num % 100
    if 11 <= last_two_digits <= 14:
        return "игр"
    if last_digit == 1:
        return "игра"
    elif last_digit in [2, 3, 4]:
        return "игры"
    else:
        return "игр"


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
    """Форматирует период отпуска в виде меток времени Discord."""
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

VOICE_ROOMS = {}
TRIGGER_CHANNEL_ARMY = None
TRIGGER_CHANNEL_PUBLIC = None
# Блокировки по member.id для защиты от двойного создания временных комнат
VOICE_ROOM_CREATION_LOCKS = {}

# Время последней проверки таблицы (защита от двойного запуска scheduler'ом)
LAST_SPREADSHEET_CHECK_TIME = None

VACATION_RULES = es("""

Боец, если ты будешь отсутствовать более 7 дней, оформи отпуск, чтобы не быть исключённым из клана за низкую активность!

**📌 Основные правила:**
* Отпуск оформляется на срок от **7 дней до 1 месяца**
* Рапорт можно продлить, создав новый со следующего дня после окончания предыдущего
* После оформления отпуск должен быть **утверждён комбатом или заместителем**
* Во время отпуска тебе **не нужно отмечаться в расписании на игры**
* Боец в отпуске **лишается возможности участия в играх** до закрытия отпуска

**✅ Уважительные причины:**
* Командировки и мероприятия по работе
* Семейные мероприятия
* Проблемы со здоровьем
* Длительные учебные мероприятия (например, сессия)

**❌ Неуважительные причины:**
* Усталость от игры

Боец, указывай честную и конкретную причину! Это помогает командованию планировать состав на игры. Отпуск может быть аннулирован, если вы будете находится в отпуске, но постоянно играть в игры во время проводимых мероприятий в клане.
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


async def load_clan_members_from_sheet():
    global CLAN_MEMBERS_CACHE, CLAN_MEMBERS_CACHE_TIME
    current_time = datetime.now().timestamp()
    if CLAN_MEMBERS_CACHE and CLAN_MEMBERS_CACHE_TIME and (current_time - CLAN_MEMBERS_CACHE_TIME) < CLAN_MEMBERS_CACHE_TTL:
        return CLAN_MEMBERS_CACHE
    if not gc:
        print("⚠️ Google Sheets не инициализирован, использую кэш")
        return CLAN_MEMBERS_CACHE
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            loop = asyncio.get_event_loop()
            spreadsheet = await loop.run_in_executor(EXECUTOR, gc.open_by_url, SPREADSHEET_URL)
            sheet = spreadsheet.worksheet(SHEET_NAME)
            data_with_colors = await get_sheet_data_with_colors(sheet, 'A1:J35')
            if not data_with_colors or len(data_with_colors) < 2:
                return CLAN_MEMBERS_CACHE
            headers = [cell['value'] for cell in data_with_colors[0]]
            rows = data_with_colors[1:]
            if NICKNAME_COLUMN not in headers:
                return CLAN_MEMBERS_CACHE
            nick_idx = headers.index(NICKNAME_COLUMN)
            members = []
            for row in rows:
                if nick_idx < len(row):
                    raw_nickname = row[nick_idx]['value'].strip()
                    nickname = extract_nickname(raw_nickname)
                    if nickname:
                        members.append(nickname)
            CLAN_MEMBERS_CACHE = members
            CLAN_MEMBERS_CACHE_TIME = current_time
            print(f"✅ Загружено {len(members)} участников клана из таблицы")
            return members
        except Exception as e:
            error_str = str(e)
            if ('503' in error_str or '500' in error_str or '429' in error_str) and attempt < max_retries - 1:
                wait_time = 2 * (attempt + 1)
                print(f"⚠️ Google API ошибка (попытка {attempt + 1}/{max_retries}), повтор через {wait_time} сек...")
                await asyncio.sleep(wait_time)
                continue
            print(f"❌ Ошибка загрузки списка клана: {e}")
            return CLAN_MEMBERS_CACHE


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


async def send_chunked(thread, text, user_name=""):
    if not text:
        return
    chunk_size = 1800
    if len(text) <= chunk_size:
        await thread.send(text)
        await asyncio.sleep(0.5)
        return
    chunks = []
    for i in range(0, len(text), chunk_size):
        chunks.append(text[i:i + chunk_size])
    for chunk in chunks:
        try:
            await thread.send(chunk)
            await asyncio.sleep(0.5)
        except Exception as e:
            print(f"Ошибка при отправке части: {e}")
            raise


# ============== ПОСТРОЕНИЕ ТЕКСТОВ ==============

def build_intro_lines(current_time: datetime) -> list:
    return [
        es("🔔 **Проверка ошибок с регистрациями** 🔔"), "",
        es("Это автоматическая проверка клана по таблице — всех, кто не в отпуске. ") +
        "**[Полная таблица](<https://enemygaming.netlify.app/temptable>)** — обновляется ежедневно.",
        "Если вы исправили какую-либо проблему, поставьте лайк как реакцию на это сообщение.",
        es("🔴 **Красные** проблемы — критические, требуют немедленного исправления."),
        es("🟡 **Желтые** проблемы — менее важные, но тоже требуют своевременного исправления."),
        es(f"📅 Проверка от {current_time.strftime('%d.%m.%Y %H:%M')} МСК"), " ",
        "─" * 50,
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
        parts.append(es("🔴 **Критические проблемы, требующие скорейшего исправления:**"))
        for issue in red_issues:
            parts.append(issue_line(issue))
        parts.append("")
    if yellow_issues:
        parts.append(es("🟡 **Важные, но менее критические проблемы, также требующие своевременного исправления:**"))
        for issue in yellow_issues:
            parts.append(issue_line(issue))
        parts.append("")
    parts.append("─" * 50)
    return "\n".join(parts).strip("\n")


async def check_spreadsheet():
    """Проверка таблицы с двойной защитой от дублирования:
    1. check_lock (asyncio.Lock) — защита от параллельного запуска
    2. LAST_SPREADSHEET_CHECK_TIME — защита от повторного запуска scheduler'ом
    """
    global LAST_SPREADSHEET_CHECK_TIME
    
    if check_lock.locked():
        return
    
    # Предварительная проверка: если с последней проверки прошло меньше 5 минут — пропускаем
    current_time = datetime.now(MSK)
    if LAST_SPREADSHEET_CHECK_TIME:
        elapsed = (current_time - LAST_SPREADSHEET_CHECK_TIME).total_seconds()
        if elapsed < 300:  # 5 минут
            print(f"⏭️ Пропуск проверки таблицы (прошло {elapsed:.0f} сек с последней)")
            return
    
    async with check_lock:
        # Повторная проверка внутри lock (защита от race condition)
        current_time = datetime.now(MSK)
        if LAST_SPREADSHEET_CHECK_TIME:
            elapsed = (current_time - LAST_SPREADSHEET_CHECK_TIME).total_seconds()
            if elapsed < 300:
                return
        
        # Запоминаем время начала проверки
        LAST_SPREADSHEET_CHECK_TIME = current_time
        
        try:
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
            thread = await client.fetch_channel(THREAD_ID)
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
            if user_issues or users_not_found:
                intro = build_intro_message(current_time)
                if len(intro) <= EXPECTED_INTRO_MAX_LEN:
                    await send_chunked(thread, intro, "вводное сообщение")
                for discord_user, issues in user_issues.items():
                    user_msg = build_user_message(discord_user, issues)
                    await send_chunked(thread, user_msg, discord_user.display_name)
                if users_not_found:
                    not_found_msg = ("─" * 50 + "\n\n" + es("⚠️ **Не удалось найти в Discord:**\n") + ", ".join(users_not_found))
                    await send_chunked(thread, not_found_msg, "список ненайденных")
        except Exception as e:
            print(f"Ошибка при проверке: {e}")


# ============== РАБОТА С ДАННЫМИ ==============

def load_json(filename, default=None):
    if default is None:
        default = {}
    if not os.path.exists(filename):
        return default
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def save_json(filename, data):
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Ошибка сохранения {filename}: {e}")


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


async def get_active_members(current_date: datetime) -> list:
    members = await load_clan_members_from_sheet()
    return [m for m in members if not is_on_vacation_dynamic(m, current_date)]


async def get_vacation_role(guild):
    role = discord.utils.get(guild.roles, name="Отпуск")
    if not role:
        try:
            role = await guild.create_role(name="Отпуск", mentionable=False)
        except Exception:
            return None
    return role


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
    def __init__(self, image_key='none'):
        super().__init__(title=es("📅 Создание мероприятия"))
        self.image_key = image_key
        self.event_title = discord.ui.TextInput(label="Название мероприятия", required=True, max_length=100)
        self.event_description = discord.ui.TextInput(label="Описание", style=discord.TextStyle.paragraph, required=True, max_length=1000)
        self.start_time = discord.ui.TextInput(label="Начало (ДД.ММ.ГГГГ ЧЧ:ММ)", required=True, max_length=16)
        self.end_time = discord.ui.TextInput(label="Окончание (ДД.ММ.ГГГГ ЧЧ:ММ)", required=True, max_length=16)
        self.num_games = discord.ui.TextInput(label="Количество игр (0, 1, 2 или больше)", required=False, max_length=2, default="0")
        self.add_item(self.event_title)
        self.add_item(self.event_description)
        self.add_item(self.start_time)
        self.add_item(self.end_time)
        self.add_item(self.num_games)
    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            start = MSK.localize(datetime.strptime(self.start_time.value, "%d.%m.%Y %H:%M"))
            end = MSK.localize(datetime.strptime(self.end_time.value, "%d.%m.%Y %H:%M"))
            games = int(self.num_games.value.strip() or "0")
            if games < 0 or games > MAX_GAMES:
                await interaction.followup.send(es(f"❌ Количество игр: 0-{MAX_GAMES}!"), ephemeral=True)
                return
            await create_event(self.event_title.value, self.event_description.value, start, end, image_key=self.image_key, num_games=games)
            await interaction.followup.send(es("✅ Мероприятие создано!"), ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)


class EventEditModal(discord.ui.Modal):
    def __init__(self, event_id, current_title, current_description, current_start, current_end, image_key='none', num_games=0):
        super().__init__(title=es("✏️ Редактирование мероприятия"))
        self.event_id = event_id
        self.image_key = image_key
        self.event_title = discord.ui.TextInput(label="Название мероприятия", default=current_title, required=True, max_length=100)
        self.event_description = discord.ui.TextInput(label="Описание", style=discord.TextStyle.paragraph, default=current_description, required=True, max_length=1000)
        self.start_time = discord.ui.TextInput(label="Начало (ДД.ММ.ГГГГ ЧЧ:ММ)", default=current_start, required=True, max_length=16)
        self.end_time = discord.ui.TextInput(label="Окончание (ДД.ММ.ГГГГ ЧЧ:ММ)", default=current_end, required=True, max_length=16)
        self.num_games = discord.ui.TextInput(label="Количество игр (0, 1, 2 или больше)", default=str(num_games), required=False, max_length=2)
        self.add_item(self.event_title)
        self.add_item(self.event_description)
        self.add_item(self.start_time)
        self.add_item(self.end_time)
        self.add_item(self.num_games)
    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            start = MSK.localize(datetime.strptime(self.start_time.value, "%d.%m.%Y %H:%M"))
            end = MSK.localize(datetime.strptime(self.end_time.value, "%d.%m.%Y %H:%M"))
            games = int(self.num_games.value.strip() or "0")
            if games < 0 or games > MAX_GAMES:
                await interaction.followup.send(es(f"❌ Количество игр: 0-{MAX_GAMES}!"), ephemeral=True)
                return
            await update_event(self.event_id, self.event_title.value, self.event_description.value, start, end, image_key=self.image_key, num_games=games)
            await interaction.followup.send(es("✅ Мероприятие обновлено!"), ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)


class EventImageSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        options = [discord.SelectOption(label="Без картинки", value="none", emoji="🚫")]
        for key, data in EVENT_IMAGES.items():
            options.append(discord.SelectOption(label=data['title'], value=key, emoji="🖼️"))
        self.select = discord.ui.Select(placeholder="🖼️ Выберите картинку для мероприятия...", options=options)
        self.select.callback = self.select_callback
        self.add_item(self.select)
    async def select_callback(self, interaction):
        selected_key = self.select.values[0]
        self.stop()
        await interaction.response.send_modal(EventCreateModal(image_key=selected_key))


class EventEditSelectView(discord.ui.View):
    def __init__(self, event_id):
        super().__init__(timeout=120)
        self.event_id = event_id
        events = load_json(EVENTS_FILE, {})
        current_image = events.get(event_id, {}).get('image_key', 'none')
        options = [discord.SelectOption(label="Без картинки", value="none", emoji="🚫", default=(current_image == 'none'))]
        for key, data in EVENT_IMAGES.items():
            options.append(discord.SelectOption(label=data['title'], value=key, emoji="🖼️", default=(key == current_image)))
        self.select = discord.ui.Select(placeholder="🖼️ Выберите картинку...", options=options)
        self.select.callback = self.select_callback
        self.add_item(self.select)
    async def select_callback(self, interaction):
        selected_key = self.select.values[0]
        self.stop()
        await open_edit_modal(interaction, self.event_id, image_key=selected_key)


class AdminMainMenuView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(label=es("📅 Создать мероприятие"), style=discord.ButtonStyle.primary, custom_id="admin_create_event", row=0)
    async def create_event_button(self, interaction, button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        view = EventImageSelectView()
        await interaction.response.send_message(es("🖼️ Выберите картинку для мероприятия:"), view=view, ephemeral=True)
    
    @discord.ui.button(label=es("📋 Список мероприятий"), style=discord.ButtonStyle.secondary, custom_id="admin_event_list", row=0)
    async def event_list_button(self, interaction, button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        await show_event_list(interaction)
    
    @discord.ui.button(label=es("📝 Отправить сообщение"), style=discord.ButtonStyle.success, custom_id="admin_send_message", row=1)
    async def send_message_button(self, interaction, button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        await interaction.response.send_modal(SendMessageModal())
    
    @discord.ui.button(label=es("🗑️ Удалить сообщение"), style=discord.ButtonStyle.danger, custom_id="admin_delete_message", row=1)
    async def delete_message_button(self, interaction, button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        await interaction.response.send_modal(DeleteMessageModal())
    
    @discord.ui.button(label=es("🏖️ Отпуск для бойца"), style=discord.ButtonStyle.primary, custom_id="admin_vacation_for_player", row=2)
    async def vacation_for_player_button(self, interaction, button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        await interaction.response.send_modal(AdminVacationModal())
    
    @discord.ui.button(label=es("🏖️ Список отпусков"), style=discord.ButtonStyle.secondary, custom_id="admin_vacation_list", row=2)
    async def vacation_list_button(self, interaction, button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        await show_vacation_list(interaction)
    
    @discord.ui.button(label=es("🔍 Проверить таблицу"), style=discord.ButtonStyle.success, custom_id="admin_check_table", row=3)
    async def check_table_button(self, interaction, button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        if check_lock.locked():
            await interaction.response.send_message(es("⚠️ Проверка уже выполняется!"), ephemeral=True)
            return
        # defer СРАЗУ, чтобы Discord не отклонил interaction через 3 секунды
        await interaction.response.defer(ephemeral=True, thinking=True)
        await check_spreadsheet()
        await interaction.followup.send(es("✅ Проверка таблицы завершена!"), ephemeral=True)
    
    @discord.ui.button(label=es("🔍 Извлечь код сообщения"), style=discord.ButtonStyle.secondary, custom_id="admin_extract_message", row=3)
    async def extract_message_button(self, interaction, button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        await interaction.response.send_modal(ExtractMessageModal())
    
    @discord.ui.button(label=es("🔄 Обновить шаблоны сообщений"), style=discord.ButtonStyle.primary, custom_id="admin_refresh_templates", row=3)
    async def refresh_templates_button(self, interaction, button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        # defer СРАЗУ — обновление шаблонов занимает много времени
        await interaction.response.defer(ephemeral=True, thinking=True)
        ev_updated, ev_errors, vac_updated, vac_errors = await update_all_templates()
        await interaction.followup.send(
            es(f"🔄 **Обновление завершено!**\n\n"
               f"📅 Мероприятий обновлено: **{ev_updated}** (ошибок: {ev_errors})\n"
               f"🏖️ Отпусков обновлено: **{vac_updated}** (ошибок: {vac_errors})"),
            ephemeral=True
        )


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


class EventView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    def get_event_id_by_message(self, interaction):
        events = load_json(EVENTS_FILE, {})
        for event_id, event in events.items():
            if event.get('message_id') == interaction.message.id:
                return event_id
        return None
    @discord.ui.button(label=es("✅ Приду"), style=discord.ButtonStyle.success, custom_id="event_accept", row=0)
    async def accept_button(self, interaction, button):
        event_id = self.get_event_id_by_message(interaction)
        if not event_id:
            await interaction.response.send_message(es("❌ Мероприятие не найдено!"), ephemeral=True)
            return
        await handle_event_response(interaction, event_id, "accept")
    @discord.ui.button(label=es("❌ Не приду"), style=discord.ButtonStyle.danger, custom_id="event_decline", row=0)
    async def decline_button(self, interaction, button):
        event_id = self.get_event_id_by_message(interaction)
        if not event_id:
            await interaction.response.send_message(es("❌ Мероприятие не найдено!"), ephemeral=True)
            return
        await handle_event_response(interaction, event_id, "decline")
    @discord.ui.button(label=es("✏️ Редактировать"), style=discord.ButtonStyle.secondary, custom_id="event_edit", row=1)
    async def edit_button(self, interaction, button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        event_id = self.get_event_id_by_message(interaction)
        if not event_id:
            await interaction.response.send_message(es("❌ Мероприятие не найдено!"), ephemeral=True)
            return
        await interaction.response.send_message(es("🖼️ Выберите картинку (или оставьте текущую):"), view=EventEditSelectView(event_id), ephemeral=True)
    @discord.ui.button(label=es("📝 Указать явку бойцов"), style=discord.ButtonStyle.success, custom_id="event_attendance", row=1)
    async def attendance_button(self, interaction, button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        event_id = self.get_event_id_by_message(interaction)
        if not event_id:
            await interaction.response.send_message(es("❌ Мероприятие не найдено!"), ephemeral=True)
            return
        # defer СРАЗУ — start_attendance_wizard загружает список клана (может быть долго)
        await interaction.response.defer(ephemeral=True, thinking=True)
        await start_attendance_wizard(interaction, event_id)
    @discord.ui.button(label=es("❌ Отменить мероприятие"), style=discord.ButtonStyle.danger, custom_id="event_cancel", row=2)
    async def cancel_button(self, interaction, button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        event_id = self.get_event_id_by_message(interaction)
        if not event_id:
            await interaction.response.send_message(es("❌ Мероприятие не найдено!"), ephemeral=True)
            return
        await cancel_event(interaction, event_id)


# ============== МАСТЕР УЧЁТА ЯВКИ (с командирами отделений) ==============

class AttendanceWizard:
    def __init__(self, event_id, num_games, event_title):
        self.event_id = event_id
        self.num_games = num_games
        self.event_title = event_title
        self.data = {}
        self.commanders = {}
        self.current_step = 0
        self.phase = 'players'


class CommanderSelectView(discord.ui.View):
    """Select для выбора ОДНОГО командира отделения"""
    def __init__(self, wizard, clan_members):
        super().__init__(timeout=300)
        self.wizard = wizard
        self.select = None
        
        options = [discord.SelectOption(label="— Без командира —", value="none", emoji="🚫")]
        for nick in clan_members[:MAX_SELECT_OPTIONS - 1]:
            options.append(discord.SelectOption(label=nick, value=nick))
        
        self.select = discord.ui.Select(
            placeholder="🪖 Выберите командира отделения...",
            options=options,
            min_values=1,
            max_values=1,
            custom_id=f"commander_select_{wizard.current_step}"
        )
        self.select.callback = self.select_callback
        self.add_item(self.select)
        
        skip_btn = discord.ui.Button(label=es("⏭️ Пропустить (без командира)"), style=discord.ButtonStyle.secondary, custom_id="commander_skip", row=2)
        skip_btn.callback = self.skip_callback
        self.add_item(skip_btn)
    
    async def select_callback(self, interaction):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Только комбат или заместитель!"), ephemeral=True)
            return
        selected = self.select.values[0]
        if selected == "none":
            commander = None
        else:
            commander = selected
        
        if self.wizard.num_games == 0:
            self.wizard.commanders["overall"] = commander
        else:
            self.wizard.commanders[self.wizard.current_step] = commander
        
        self.stop()
        await interaction.response.defer()
        await proceed_to_next_step(interaction, self.wizard)
    
    async def skip_callback(self, interaction):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Только комбат или заместитель!"), ephemeral=True)
            return
        if self.wizard.num_games == 0:
            self.wizard.commanders["overall"] = None
        else:
            self.wizard.commanders[self.wizard.current_step] = None
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
            skip_btn = discord.ui.Button(label=es("⏭️ Пропустить эту игру"), style=discord.ButtonStyle.secondary, custom_id=f"attendance_skip_{step}", row=4)
            skip_btn.callback = self.skip_callback
            self.add_item(skip_btn)
            next_btn = discord.ui.Button(label=es(f"➡️ Далее (командир отделения)"), style=discord.ButtonStyle.primary, custom_id=f"attendance_next_{step}", row=4)
            next_btn.callback = self.to_commander_callback
            self.add_item(next_btn)
        else:
            skip_btn = discord.ui.Button(label=es("⏭️ Пропустить эту игру"), style=discord.ButtonStyle.secondary, custom_id=f"attendance_skip_{step}", row=4)
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
    """Запускает мастер учёта явки. Корректно работает и если interaction уже defer'нут,
    и если ещё нет."""
    events = load_json(EVENTS_FILE, {})
    event = events.get(event_id)
    if not event:
        if interaction.response.is_done():
            await interaction.followup.send(es("❌ Мероприятие не найдено!"), ephemeral=True)
        else:
            await interaction.response.send_message(es("❌ Мероприятие не найдено!"), ephemeral=True)
        return
    num_games = event.get('num_games', 0)
    wizard = AttendanceWizard(event_id, num_games, event.get('title', ''))
    clan_members = await load_clan_members_from_sheet()
    if not clan_members:
        if interaction.response.is_done():
            await interaction.followup.send(es("❌ Список клана пуст!"), ephemeral=True)
        else:
            await interaction.response.send_message(es("❌ Список клана пуст!"), ephemeral=True)
        return
    view = AttendanceStepView(wizard, 0, clan_members)
    if num_games == 0:
        title_text = es(f"👥 **{event.get('title', '')}**\n\n") + es("Выберите бойцов, явившихся на мероприятие:")
    else:
        title_text = es(f"👥 **{event.get('title', '')}**\n\n") + es(f"**Игра 1** из {num_games}\nВыберите явившихся:")
    
    # Отправляем через followup если interaction уже defer'нут, иначе через response
    if interaction.response.is_done():
        await interaction.followup.send(title_text, view=view, ephemeral=True)
    else:
        await interaction.response.send_message(title_text, view=view, ephemeral=True)


async def show_commander_step(interaction, wizard):
    clan_members = await load_clan_members_from_sheet()
    view = CommanderSelectView(wizard, clan_members)
    
    if wizard.num_games == 0:
        title_text = es(f"🪖 **{wizard.event_title}**\n\n") + es("Выберите командира отделения на этом мероприятии:")
    else:
        title_text = es(f"🪖 **{wizard.event_title}**\n\n") + es(f"**Командир отделения на игре {wizard.current_step + 1}** из {wizard.num_games}\nВыберите одного командира:")
    
    await interaction.followup.send(title_text, view=view, ephemeral=True)


async def proceed_to_next_step(interaction, wizard):
    clan_members = await load_clan_members_from_sheet()
    
    if wizard.num_games == 0:
        await finalize_attendance(interaction, wizard)
        return
    
    wizard.current_step += 1
    
    if wizard.current_step >= wizard.num_games:
        await finalize_attendance(interaction, wizard)
        return
    
    view = AttendanceStepView(wizard, wizard.current_step, clan_members)
    title_text = es(f"👥 **{wizard.event_title}**\n\n") + es(f"**Игра {wizard.current_step + 1}** из {wizard.num_games}\nВыберите явившихся:")
    await interaction.followup.send(title_text, view=view, ephemeral=True)


async def finalize_attendance(interaction, wizard):
    events = load_json(EVENTS_FILE, {})
    event = events.get(wizard.event_id)
    if not event:
        await interaction.followup.send(es("❌ Мероприятие не найдено!"), ephemeral=True)
        return
    
    attendance = load_json(ATTENDANCE_FILE, {})
    
    if wizard.event_id in attendance:
        old_record = attendance[wizard.event_id]
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
        record['overall_commander'] = wizard.commanders.get('overall')
    else:
        record['games'] = {}
        for i in range(wizard.num_games):
            record['games'][str(i+1)] = {
                'players': wizard.data.get(i, []),
                'commander': wizard.commanders.get(i)
            }
    
    thread = await get_or_create_thread(event, wizard.event_id, wizard.event_title)
    if not thread:
        await interaction.followup.send(es("❌ Не удалось получить ветку мероприятия!"), ephemeral=True)
        return
    
    record['thread_id'] = thread.id
    
    report_text = es(f"🏆 **Отчёт о явке: {wizard.event_title}**\n\n")
    report_text += es(f"📋 Составил: **{interaction.user.display_name}**\n\n")
    
    if wizard.num_games == 0:
        players = wizard.data.get('overall', [])
        commander = wizard.commanders.get('overall')
        
        report_text += es(f"👥 **Явились на мероприятие ({len(players)}):**\n")
        if players:
            report_text += "\n".join(players)
        else:
            report_text += es("*Никто не явился*")
        
        report_text += "\n\n"
        report_text += es("🪖 **Командир отделения:**\n")
        if commander:
            report_text += commander
        else:
            report_text += es("*Не назначен*")
    else:
        for i in range(wizard.num_games):
            players = wizard.data.get(i, [])
            commander = wizard.commanders.get(i)
            
            report_text += es(f"🎮 **Игра {i+1}**\n\n")
            
            report_text += es(f"👥 Явились ({len(players)}):\n")
            if players:
                report_text += "\n".join(players)
            else:
                report_text += es("*Никто не явился*")
            
            report_text += "\n\n"
            report_text += es(f"🪖 Командир отделения:\n")
            if commander:
                report_text += commander
            else:
                report_text += es("*Не назначен*")
            
            if i < wizard.num_games - 1:
                report_text += "\n\n"
    
    new_msg = await thread.send(report_text)
    record['attendance_message_id'] = new_msg.id
    
    attendance[wizard.event_id] = record
    save_json(ATTENDANCE_FILE, attendance)
    
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
        embed.add_field(name=es("ℹ️ Статус"), value=es("⏳ Ожидает утверждения комбатом"), inline=False)
        embed.set_footer(text="Комбат или заместитель: утвердите или отклоните отпуск")
        message = await channel.send(embed=embed, view=VacationApprovalView())
        vacations[nickname]['message_id'] = message.id
        vacations[nickname]['channel_id'] = channel.id
        try:
            thread = await message.create_thread(name=f"💬 Утверждение отпуска - {nickname}")
            guild = channel.guild
            mentions = []
            role_kombat = discord.utils.get(guild.roles, name="Комбат ArmA")
            role_zam = discord.utils.get(guild.roles, name="Зам. комбата ArmA")
            if role_kombat:
                mentions.append(role_kombat.mention)
            if role_zam:
                mentions.append(role_zam.mention)
            vacation_mention = f"<#{VACATION_CHANNEL_ID}>"
            if mentions:
                await thread.send(f"{' '.join(mentions)}\n\n" + es(f"📋 Новый запрос на отпуск от **{nickname}**!\n\n") + es(f"👉 Перейдите в канал {vacation_mention} и рассмотрите рапорт."))
            else:
                await thread.send(es(f"📋 Новый запрос на отпуск от **{nickname}**!\n\n") + es(f"👉 Перейдите в канал {vacation_mention}."))
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
            embed.description = f"Отпуск для **{nickname}**" if vacation.get('by_admin') else f"**{nickname}** взял(а) отпуск"
            for i, field in enumerate(embed.fields):
                if field.name == es("📅 Период"):
                    embed.set_field_at(i, name=es("📅 Период"), value=format_vacation_period(vacation['start'], vacation['end']), inline=False)
                    break
                elif field.name == es("ℹ️ Статус"):
                    embed.set_field_at(i, name=es("ℹ️ Статус"), value=es("✅ Утверждён и активен"), inline=False)
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
                    embed.set_field_at(i, name=es("ℹ️ Статус"), value=es("❌ Отклонён командованием"), inline=False)
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
    vacation['status'] = 'ended_early' if early else 'ended_scheduled'
    vacation['closed_at'] = datetime.now(MSK).isoformat()
    vacation['closed_by'] = interaction.user.display_name
    save_json(VACATIONS_FILE, vacations)
    member = await find_member_by_nickname(nickname)
    if member:
        await update_vacation_role(member, False)
    try:
        channel = await client.fetch_channel(vacation['channel_id'])
        message = await channel.fetch_message(vacation['message_id'])
        if message.embeds:
            embed = message.embeds[0]
            status_text = es("❌ Завершен досрочно") if early else es("✅ Завершен по истечению срока")
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
    await interaction.response.send_message(f"✅ Отпуск {nickname} закрыт.", ephemeral=True)


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
                                    embed.set_field_at(i, name=es("ℹ️ Статус"), value=es("✅ Завершен по истечению срока"), inline=False)
                                elif field.name == es("📅 Период"):
                                    embed.set_field_at(i, name=es("📅 Период"), value=format_vacation_period(data['start'], data['end']), inline=False)
                            embed.color = discord.Color.greyple()
                            await message.edit(embed=embed, view=None)
                    except Exception:
                        pass
                print(f"✅ Отпуск {nickname} автоматически закрыт (истёк срок)")
        except Exception:
            pass
    if changed:
        save_json(VACATIONS_FILE, vacations)


# ============== ФУНКЦИИ МЕРОПРИЯТИЙ ==============

async def handle_event_response(interaction, event_id, response_type):
    """Обработка ответа на мероприятие с защитой от Unknown interaction и Already acknowledged."""
    events = load_json(EVENTS_FILE, {})
    
    if event_id not in events:
        if not interaction.response.is_done():
            await interaction.response.send_message(es("❌ Мероприятие не найдено!"), ephemeral=True)
        return
    
    event = events[event_id]
    nickname = interaction.user.display_name
    current_date = datetime.now(MSK)
    event_end = datetime.fromtimestamp(event['end_time'], MSK)
    
    if current_date > event_end:
        if not interaction.response.is_done():
            await interaction.response.send_message(es("⛔ Мероприятие уже завершено. Отметки больше не принимаются!"), ephemeral=True)
        return
    
    if is_on_vacation_dynamic(nickname, current_date):
        if not interaction.response.is_done():
            await interaction.response.send_message(es("🏖️ Вы сейчас в отпуске."), ephemeral=True)
        return
    
    # defer СРАЗУ если ещё не отвечено, чтобы Discord не отклонил interaction через 3 секунды
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
    
    # Обновляем данные мероприятия
    if response_type == "accept":
        event['accepted'][nickname] = True
        event['declined'].pop(nickname, None)
        response_text = es("✅ Вы записаны на мероприятие!")
    else:
        event['declined'][nickname] = True
        event['accepted'].pop(nickname, None)
        response_text = es("❌ Вы отказались от участия!")
    
    save_json(EVENTS_FILE, events)
    
    # Обновляем embed сообщение
    try:
        channel = await client.fetch_channel(event['channel_id'])
        message = await channel.fetch_message(event['message_id'])
        embed = await build_event_embed(event_id)
        filename, path = get_image_info(event.get('image_key', 'none'))
        if filename and path:
            await message.edit(embed=embed, attachments=[discord.File(path, filename=filename)])
        else:
            await message.edit(embed=embed, attachments=[])
    except Exception:
        pass
    
    # Отправляем финальный ответ через followup (т.к. interaction уже defer'нут)
    await interaction.followup.send(response_text, ephemeral=True)


async def open_edit_modal(interaction, event_id, image_key=None):
    events = load_json(EVENTS_FILE, {})
    if event_id not in events:
        await interaction.response.send_message(es("❌ Мероприятие не найдено!"), ephemeral=True)
        return
    event = events[event_id]
    if image_key is None:
        image_key = event.get('image_key', 'none')
    start = datetime.fromtimestamp(event['start_time'], MSK).strftime("%d.%m.%Y %H:%M")
    end = datetime.fromtimestamp(event['end_time'], MSK).strftime("%d.%m.%Y %H:%M")
    await interaction.response.send_modal(EventEditModal(
        event_id=event_id, current_title=event['title'],
        current_description=event['description'], current_start=start, current_end=end,
        image_key=image_key, num_games=event.get('num_games', 0)
    ))


async def update_event(event_id, title, description, start_time, end_time, image_key=None, num_games=None):
    events = load_json(EVENTS_FILE, {})
    if event_id not in events:
        return
    event = events[event_id]
    event['title'] = title
    event['description'] = description
    event['start_time'] = int(start_time.timestamp())
    event['end_time'] = int(end_time.timestamp())
    if image_key is not None:
        event['image_key'] = image_key
    if num_games is not None:
        event['num_games'] = num_games
    event['reminder_2days_sent'] = False
    event['reminder_15min_sent'] = False
    save_json(EVENTS_FILE, events)
    try:
        channel = await client.fetch_channel(event['channel_id'])
        message = await channel.fetch_message(event['message_id'])
        embed = await build_event_embed(event_id)
        filename, path = get_image_info(event.get('image_key', 'none'))
        if filename and path:
            await message.edit(embed=embed, attachments=[discord.File(path, filename=filename)])
        else:
            await message.edit(embed=embed, attachments=[])
    except Exception:
        pass


async def cancel_event(interaction, event_id):
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
    await interaction.response.send_message(es("✅ Мероприятие отменено!"), ephemeral=True)


async def build_event_embed(event_id: str) -> discord.Embed:
    events = load_json(EVENTS_FILE, {})
    event = events[event_id]
    current_date = datetime.now(MSK)
    active_members = await get_active_members(current_date)
    accepted = list(event.get('accepted', {}).keys())
    declined = list(event.get('declined', {}).keys())
    unmarked = [m for m in active_members if m not in accepted and m not in declined]
    embed = discord.Embed(title=event['title'], description=event['description'], color=event.get('color', 15844367))
    
    start_ts = int(event['start_time'])
    end_ts = int(event['end_time'])
    event_end = datetime.fromtimestamp(end_ts, MSK)
    
    time_value = f"Дата: <t:{start_ts}:F> - <t:{end_ts}:t>"
    if current_date <= event_end:
        time_value += f"\nНачнется: <t:{start_ts}:R>"
    
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
    num_games = event.get('num_games', 0)
    if num_games and num_games > 0:
        games_word = pluralize_games(num_games)
        embed.add_field(name="", value=f"*На мероприятии запланировано {num_games} {games_word}*", inline=False)
    else:
        embed.add_field(name="", value="*На этом мероприятии игры не запланированы*", inline=False)
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
        thread = await message.create_thread(name=f"💬 {title}")
        events = load_json(EVENTS_FILE, {})
        if event_id in events:
            events[event_id]['thread_id'] = thread.id
            save_json(EVENTS_FILE, events)
        return thread
    except Exception:
        return None


async def create_event(title, description, start_time, end_time, image_key='none', num_games=0, color=15844367):
    event_id = str(uuid.uuid4())
    events = load_json(EVENTS_FILE, {})
    events[event_id] = {
        'title': title, 'description': description,
        'start_time': int(start_time.timestamp()), 'end_time': int(end_time.timestamp()),
        'accepted': {}, 'declined': {},
        'image_key': image_key, 'num_games': num_games, 'color': color,
        'channel_id': EVENTS_CHANNEL_ID, 'message_id': None, 'thread_id': None,
        'reminder_2days_sent': False, 'reminder_15min_sent': False
    }
    save_json(EVENTS_FILE, events)
    try:
        channel = await client.fetch_channel(EVENTS_CHANNEL_ID)
        guild = channel.guild
        embed = await build_event_embed(event_id)
        view = EventView()
        filename, path = get_image_info(image_key)
        if filename and path:
            message = await channel.send(embed=embed, view=view, file=discord.File(path, filename=filename))
        else:
            message = await channel.send(embed=embed, view=view)
        events[event_id]['message_id'] = message.id
        thread = await message.create_thread(name=f"💬 {title}")
        events[event_id]['thread_id'] = thread.id
        role = discord.utils.get(guild.roles, name="Боец ArmA")
        if role:
            await thread.send(f"{role.mention}\n\n" + es("📢 Бойцы, запланировано мероприятие! Ждем ваших отметок!"))
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
        text += f"**{event['title']}**\nID: `{event_id}`\n"
        text += f"Дата: {start.strftime('%d.%m.%Y %H:%M')}\n"
        num_games = event.get('num_games', 0)
        if num_games and num_games > 0:
            text += es(f"🎮 Игр: {num_games}\n")
        text += es(f"✅ Придут: {len(event.get('accepted', {}))}\n")
        text += es(f"❌ Не придут: {len(event.get('declined', {}))}\n\n")
    await interaction.response.send_message(text, ephemeral=True)


async def post_weekly_events():
    today = datetime.now(MSK)
    days_until_saturday = (5 - today.weekday()) % 7
    if days_until_saturday == 0 and today.hour >= 19:
        days_until_saturday = 7
    saturday = today + timedelta(days=days_until_saturday)
    saturday_start = saturday.replace(hour=16, minute=30, second=0, microsecond=0)
    saturday_end = saturday.replace(hour=19, minute=30, second=0, microsecond=0)
    sunday = saturday + timedelta(days=1)
    sunday_start = sunday.replace(hour=17, minute=45, second=0, microsecond=0)
    sunday_end = sunday.replace(hour=22, minute=15, second=0, microsecond=0)
    voice_mention = f"<#{VOICE_CHANNEL_ID}>"
    await create_event("СУББОТА - RTvT на AS VDV",
        f"Бойцы, на субботу запланировано мероприятие на сервере AS VDV. Ждём вас! Заходите в голосовой канал {voice_mention} за 15 минут до начала.",
        saturday_start, saturday_end, image_key='asvdv', num_games=2)
    await asyncio.sleep(2)
    await create_event("ВОСКРЕСЕНЬЕ - TvT TT",
        f"Бойцы, на воскресенье запланировано мероприятие на сервере TT. Ждём вас! Заходите в голосовой канал {voice_mention} за 15 минут до начала.",
        sunday_start, sunday_end, image_key='tt', num_games=3)


async def check_event_reminders():
    events = load_json(EVENTS_FILE, {})
    current_time = datetime.now(MSK)
    changed = False
    for event_id, event in events.items():
        try:
            event_start = datetime.fromtimestamp(event['start_time'], MSK)
            time_until_start = event_start - current_time
            if not event.get('reminder_2days_sent', False):
                if timedelta(hours=47) <= time_until_start <= timedelta(hours=48):
                    active_members = await get_active_members(current_time)
                    accepted = list(event.get('accepted', {}).keys())
                    declined = list(event.get('declined', {}).keys())
                    unmarked = [m for m in active_members if m not in accepted and m not in declined]
                    if unmarked:
                        thread = await get_or_create_thread(event, event_id, event['title'])
                        if thread:
                            mentions = []
                            for nickname in unmarked:
                                member = await find_member_by_nickname(nickname)
                                mentions.append(member.mention if member else f"**{nickname}**")
                            voice_mention = f"<#{VOICE_CHANNEL_ID}>"
                            reminder_text = (es(f"📋 **Внимание: {event['title']}**\n\n") +
                                " ".join(mentions) + "\n\n" +
                                es("⏳ До мероприятия осталось **2 суток**, а вы ещё не отметились!\n\n") +
                                es("👉 Пожалуйста, отметьтесь в основном посте мероприятия.\n\n") +
                                f"Сбор в голосовом канале {voice_mention}.")
                            await thread.send(reminder_text)
                    event['reminder_2days_sent'] = True
                    changed = True
            if not event.get('reminder_15min_sent', False):
                if timedelta(0) <= time_until_start <= timedelta(minutes=15):
                    accepted = list(event.get('accepted', {}).keys())
                    if accepted:
                        thread = await get_or_create_thread(event, event_id, event['title'])
                        if thread:
                            mentions = []
                            for nickname in accepted:
                                member = await find_member_by_nickname(nickname)
                                mentions.append(member.mention if member else f"**{nickname}**")
                            voice_mention = f"<#{VOICE_CHANNEL_ID}>"
                            reminder_text = (es(f"🔔 **Бойцы, внимание: {event['title']}** 🔔\n\n") +
                                " ".join(mentions) + "\n\n" +
                                es(f"⚡ Мероприятие начнется через **{int(time_until_start.total_seconds() // 60)} минут**!\n\n") +
                                es("📍 Ждем всех на сборах! Заходите в голосовой канал:\n") +
                                es(f"👉 **{voice_mention}**"))
                            await thread.send(reminder_text)
                    event['reminder_15min_sent'] = True
                    changed = True
        except Exception:
            pass
    if changed:
        save_json(EVENTS_FILE, events)


# ============== ОБНОВЛЕНИЕ ШАБЛОНОВ СООБЩЕНИЙ ==============

async def update_all_templates():
    """Обновляет шаблоны всех сообщений бота:
    - все сообщения мероприятий
    - все сообщения отпусков (и активные, и завершённые, и отклонённые)
    - правила отпусков в канале отпусков
    """
    events = load_json(EVENTS_FILE, {})
    vacations = load_json(VACATIONS_FILE, {})
    
    ev_updated = 0
    ev_errors = 0
    vac_updated = 0
    vac_errors = 0
    
    # === 1. ОБНОВЛЕНИЕ СООБЩЕНИЙ МЕРОПРИЯТИЙ ===
    for event_id, event in events.items():
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
                await message.edit(embed=embed, attachments=[discord.File(path, filename=filename)], view=EventView())
            else:
                await message.edit(embed=embed, attachments=[], view=EventView())
            ev_updated += 1
            await asyncio.sleep(4)  # Защита от rate limit
        except discord.NotFound:
            ev_errors += 1
        except Exception as e:
            print(f"❌ Ошибка обновления мероприятия '{event.get('title', '?')}': {e}")
            ev_errors += 1
    
    save_json(EVENTS_FILE, events)
    
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
            
            for i, field in enumerate(embed.fields):
                if field.name == es("📅 Период"):
                    new_period = format_vacation_period(data['start'], data['end'])
                    embed.set_field_at(i, name=es("📅 Период"), value=new_period, inline=False)
                    break
            
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
            
            if status == 'pending':
                embed.title = es("🏖️ Отпуск требует утверждения")
                embed.description = f"Отпуск для **{nickname}**" if by_admin else f"**{nickname}** запросил(а) отпуск"
                embed.color = discord.Color.orange()
                for i, field in enumerate(embed.fields):
                    if field.name == es("ℹ️ Статус"):
                        embed.set_field_at(i, name=es("ℹ️ Статус"), value=es("⏳ Ожидает утверждения комбатом"), inline=False)
                        break
                await message.edit(embed=embed, view=VacationApprovalView())
                
            elif status == 'active':
                embed.title = es("🏖️ Отпуск утверждён")
                embed.description = f"Отпуск для **{nickname}**" if by_admin else f"**{nickname}** взял(а) отпуск"
                embed.color = discord.Color.green()
                for i, field in enumerate(embed.fields):
                    if field.name == es("ℹ️ Статус"):
                        embed.set_field_at(i, name=es("ℹ️ Статус"), value=es("✅ Утверждён и активен"), inline=False)
                        break
                await message.edit(embed=embed, view=VacationMessageView())
                
            elif status in ['rejected', 'ended_early', 'ended_scheduled']:
                if status == 'rejected':
                    embed.title = es("❌ Отпуск отклонён")
                    embed.description = f"Отпуск для **{nickname}** отклонён" if by_admin else f"Запрос на отпуск **{nickname}** отклонён"
                    embed.color = discord.Color.red()
                    status_text = es("❌ Отклонён командованием")
                elif status == 'ended_early':
                    embed.title = es("🏖️ Отпуск утверждён")
                    embed.description = f"Отпуск для **{nickname}**" if by_admin else f"**{nickname}** взял(а) отпуск"
                    embed.color = discord.Color.red()
                    status_text = es("❌ Завершен досрочно")
                else:
                    embed.title = es("🏖️ Отпуск утверждён")
                    embed.description = f"Отпуск для **{nickname}**" if by_admin else f"**{nickname}** взял(а) отпуск"
                    embed.color = discord.Color.greyple()
                    status_text = es("✅ Завершен по истечению срока")
                
                for i, field in enumerate(embed.fields):
                    if field.name == es("ℹ️ Статус"):
                        embed.set_field_at(i, name=es("ℹ️ Статус"), value=status_text, inline=False)
                        break
                
                await message.edit(embed=embed, view=None)
            
            vac_updated += 1
            await asyncio.sleep(4)
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
    
    print(f"🔄 Итог обновления шаблонов:")
    print(f"   📅 Мероприятий: обновлено {ev_updated}, ошибок {ev_errors}")
    print(f"   🏖️ Отпусков: обновлено {vac_updated}, ошибок {vac_errors}")
    
    return ev_updated, ev_errors, vac_updated, vac_errors


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
            return
        
        if len(channel.members) == 0:
            await channel.delete(reason="Временная комната пуста — автоудаление")
            del VOICE_ROOMS[channel_id]
            print(f"🗑️ Удалена пустая временная комната (ID: {channel_id})")
    except discord.NotFound:
        if channel_id in VOICE_ROOMS:
            del VOICE_ROOMS[channel_id]
    except Exception as e:
        print(f"⚠️ Ошибка при удалении временной комнаты: {e}")


# ============== СОБЫТИЯ DISCORD ==============

@client.event
async def on_voice_state_update(member, before, after):
    """Обработчик для временных голосовых комнат с защитой от двойного создания."""
    global TRIGGER_CHANNEL_ARMY, TRIGGER_CHANNEL_PUBLIC, VOICE_ROOMS, VOICE_ROOM_CREATION_LOCKS
    
    if member.bot:
        return
    
    if after.channel and after.channel.id in [TRIGGER_CHANNEL_ARMY, TRIGGER_CHANNEL_PUBLIC]:
        if member.id not in VOICE_ROOM_CREATION_LOCKS:
            VOICE_ROOM_CREATION_LOCKS[member.id] = asyncio.Lock()
        
        async with VOICE_ROOM_CREATION_LOCKS[member.id]:
            for room_id, room_data in VOICE_ROOMS.items():
                if room_data['owner_id'] == member.id:
                    return
            
            await create_temp_voice_room(member, after.channel)
        return
    
    if before.channel and before.channel.id in VOICE_ROOMS:
        await asyncio.sleep(2)
        await cleanup_empty_temp_room(before.channel.id)


@client.event
async def on_ready():
    print(f'Бот запущен как {client.user} (PID {os.getpid()})')
    
    await load_clan_members_from_sheet()
    
    for guild in client.guilds:
        await setup_voice_room_triggers(guild)
    
    if not scheduler.get_job('spreadsheet_check'):
        scheduler.add_job(check_spreadsheet, 'cron', day='*/2', hour=18, minute=0, id='spreadsheet_check', replace_existing=True)
    if not scheduler.get_job('weekly_events'):
        scheduler.add_job(post_weekly_events, 'cron', day_of_week='mon', hour=12, minute=0, id='weekly_events', replace_existing=True)
    if not scheduler.get_job('vacation_check'):
        scheduler.add_job(check_expired_vacations, 'interval', hours=1, id='vacation_check', replace_existing=True)
    if not scheduler.get_job('event_reminders'):
        scheduler.add_job(check_event_reminders, 'interval', minutes=1, id='event_reminders', replace_existing=True)
    if not scheduler.get_job('clan_cache_refresh'):
        scheduler.add_job(load_clan_members_from_sheet, 'interval', hours=1, id='clan_cache_refresh', replace_existing=True)
    
    if not scheduler.running:
        scheduler.start()
    
    client.add_view(AdminMainMenuView())
    client.add_view(VacationRequestView())
    client.add_view(VacationApprovalView())
    client.add_view(VacationMessageView())
    client.add_view(EventView())
    
    try:
        admin_channel = await client.fetch_channel(ADMIN_CHANNEL_ID)
        try:
            await admin_channel.purge(limit=None, check=lambda m: m.author == client.user)
        except Exception:
            pass
        embed = discord.Embed(
            title=es("🛠️ Панель управления комбата и заместителей"),
            description=("Здесь вы можете управлять всеми функциями бота через кнопки. "
                        "Функции бота разделены по строчкам:\n\n"
                        "1. Мероприятия \n2. Сообщения \n3. Отпуска \n4. Утилиты \n"),
            color=discord.Color.blue()
        )
        await admin_channel.send(embed=embed, view=AdminMainMenuView())
    except Exception as e:
        print(f"⚠️ Не удалось опубликовать админ-меню: {e}")


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
        EXECUTOR.shutdown(wait=False)
        if os.path.exists(LOCK_FILE):
            try:
                with open(LOCK_FILE, 'r') as f:
                    saved_pid = f.read().strip()
                if saved_pid == str(os.getpid()):
                    os.remove(LOCK_FILE)
            except Exception:
                pass