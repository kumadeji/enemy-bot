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
        '🕹️ ': '🕹️ㅤ', '🏆 ': '🏆ㅤ',
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
    'Discord клана (с клантегом)',
    'Discord ECHO (с клантегом)',
    'Discord AS VDV (с клантегом)',
    'Discord TT (с клантегом)',
    'Steam (с клантегом)',
    'Steam (в друзьях у BURBON?)',
    'Сайт клана (без клантега)',
    'Сайт ECHO (без клантега)',
    'Сайт AS VDV (без клантега)',
    'Сайт TT (без клантега - исправить только через администрацию)'
]

EXPECTED_INTRO_MAX_LEN = 700

# ============== НОВЫЕ НАСТРОЙКИ ==============

EVENTS_CHANNEL_ID = 1311705378140196926
VACATION_CHANNEL_ID = 1284905224099598407
ADMIN_CHANNEL_ID = 1536632416511332362
VOICE_CHANNEL_ID = 1284893513921728582
VOICE_CHANNEL_URL = "https://discord.com/channels/734494109032513699/1284893513921728582"

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


CLAN_MEMBERS_CACHE = []
CLAN_MEMBERS_CACHE_TIME = None
CLAN_MEMBERS_CACHE_TTL = 3600

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


def get_sheet_data_with_colors(sheet, range_name):
    try:
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
    except Exception as e:
        print(f"❌ Ошибка при получении данных с цветами из API: {e}")
        return []


async def load_clan_members_from_sheet():
    global CLAN_MEMBERS_CACHE, CLAN_MEMBERS_CACHE_TIME
    current_time = datetime.now().timestamp()
    if CLAN_MEMBERS_CACHE and CLAN_MEMBERS_CACHE_TIME and (current_time - CLAN_MEMBERS_CACHE_TIME) < CLAN_MEMBERS_CACHE_TTL:
        return CLAN_MEMBERS_CACHE
    if not gc:
        print("⚠️ Google Sheets не инициализирован, использую кэш")
        return CLAN_MEMBERS_CACHE
    try:
        spreadsheet = gc.open_by_url(SPREADSHEET_URL)
        sheet = spreadsheet.worksheet(SHEET_NAME)
        data_with_colors = get_sheet_data_with_colors(sheet, 'A1:J28')
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
        es("🔔 **Проверка ошибок с регистрациями** 🔔"),
        "",
        es("Это автоматическая проверка клана по таблице — всех, кто не в отпуске. ") +
        "**[Полная таблица](<https://enemygaming.netlify.app/temptable>)** — обновляется ежедневно.",
        "Если вы исправили какую-либо проблему, поставьте лайк как реакцию на это сообщение.",
        es("🔴 **Красные** проблемы — критические, требуют немедленного исправления."),
        es("🟡 **Желтые** проблемы — менее важные, но тоже требуют своевременного исправления."),
        es(f"📅 Проверка от {current_time.strftime('%d.%m.%Y %H:%M')} МСК"),
        " ",
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
    if check_lock.locked():
        return
    async with check_lock:
        try:
            if not gc:
                return
            spreadsheet = gc.open_by_url(SPREADSHEET_URL)
            sheet = spreadsheet.worksheet(SHEET_NAME)
            data_with_colors = get_sheet_data_with_colors(sheet, 'A1:J28')
            if not data_with_colors or len(data_with_colors) < 2:
                return
            headers = [cell['value'] for cell in data_with_colors[0]]
            rows = data_with_colors[1:]
            current_time = datetime.now(MSK)
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
        self.num_games = discord.ui.TextInput(label="Количество игр (0 = без игр)", required=False, max_length=2, default="0")
        self.add_item(self.event_title)
        self.add_item(self.event_description)
        self.add_item(self.start_time)
        self.add_item(self.end_time)
        self.add_item(self.num_games)
    
    async def on_submit(self, interaction):
        try:
            start = MSK.localize(datetime.strptime(self.start_time.value, "%d.%m.%Y %H:%M"))
            end = MSK.localize(datetime.strptime(self.end_time.value, "%d.%m.%Y %H:%M"))
            games = int(self.num_games.value.strip() or "0")
            if games < 0 or games > MAX_GAMES:
                await interaction.response.send_message(es(f"❌ Количество игр: 0-{MAX_GAMES}!"), ephemeral=True)
                return
            await create_event(self.event_title.value, self.event_description.value, start, end, image_key=self.image_key, num_games=games)
            await interaction.response.send_message(es("✅ Мероприятие создано!"), ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)


class EventEditModal(discord.ui.Modal):
    def __init__(self, event_id, current_title, current_description, current_start, current_end, image_key='none', num_games=0):
        super().__init__(title=es("✏️ Редактирование мероприятия"))
        self.event_id = event_id
        self.image_key = image_key
        self.event_title = discord.ui.TextInput(label="Название мероприятия", default=current_title, required=True, max_length=100)
        self.event_description = discord.ui.TextInput(label="Описание", style=discord.TextStyle.paragraph, default=current_description, required=True, max_length=1000)
        self.start_time = discord.ui.TextInput(label="Начало (ДД.ММ.ГГГГ ЧЧ:ММ)", default=current_start, required=True, max_length=16)
        self.end_time = discord.ui.TextInput(label="Окончание (ДД.ММ.ГГГГ ЧЧ:ММ)", default=current_end, required=True, max_length=16)
        self.num_games = discord.ui.TextInput(label="Количество игр (0 = без игр)", default=str(num_games), required=False, max_length=2)
        self.add_item(self.event_title)
        self.add_item(self.event_description)
        self.add_item(self.start_time)
        self.add_item(self.end_time)
        self.add_item(self.num_games)
    
    async def on_submit(self, interaction):
        try:
            start = MSK.localize(datetime.strptime(self.start_time.value, "%d.%m.%Y %H:%M"))
            end = MSK.localize(datetime.strptime(self.end_time.value, "%d.%m.%Y %H:%M"))
            games = int(self.num_games.value.strip() or "0")
            if games < 0 or games > MAX_GAMES:
                await interaction.response.send_message(es(f"❌ Количество игр: 0-{MAX_GAMES}!"), ephemeral=True)
                return
            await update_event(self.event_id, self.event_title.value, self.event_description.value, start, end, image_key=self.image_key, num_games=games)
            await interaction.response.send_message(es("✅ Мероприятие обновлено!"), ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)


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
    
    # ═══════════ РЯД 0: МЕРОПРИЯТИЯ ═══════════
    
    @discord.ui.button(label=es("📅 Создать мероприятие"), style=discord.ButtonStyle.primary, custom_id="admin_create_event", row=0)
    async def create_event_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        # Сначала показываем Select для выбора картинки, затем открывается Modal
        view = EventImageSelectView()
        await interaction.response.send_message(
            es("🖼️ Выберите картинку для мероприятия:"),
            view=view,
            ephemeral=True
        )
    
    @discord.ui.button(label=es("📋 Список мероприятий"), style=discord.ButtonStyle.secondary, custom_id="admin_event_list", row=0)
    async def event_list_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        await show_event_list(interaction)
    
    # ═══════════ РЯД 1: СООБЩЕНИЯ ═══════════
    
    @discord.ui.button(label=es("📝 Отправить сообщение"), style=discord.ButtonStyle.success, custom_id="admin_send_message", row=1)
    async def send_message_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        modal = SendMessageModal()
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label=es("🗑️ Удалить сообщение"), style=discord.ButtonStyle.danger, custom_id="admin_delete_message", row=1)
    async def delete_message_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        modal = DeleteMessageModal()
        await interaction.response.send_modal(modal)
    
    # ═══════════ РЯД 2: ОТПУСКА ═══════════
    
    @discord.ui.button(label=es("🏖️ Отпуск для бойца"), style=discord.ButtonStyle.primary, custom_id="admin_vacation_for_player", row=2)
    async def vacation_for_player_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        modal = AdminVacationModal()
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label=es("🏖️ Список отпусков"), style=discord.ButtonStyle.secondary, custom_id="admin_vacation_list", row=2)
    async def vacation_list_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        await show_vacation_list(interaction)
    
    # ═══════════ РЯД 3: УТИЛИТЫ ═══════════
    
    @discord.ui.button(label=es("🔍 Проверить таблицу"), style=discord.ButtonStyle.success, custom_id="admin_check_table", row=3)
    async def check_table_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        if check_lock.locked():
            await interaction.response.send_message(es("⚠️ Проверка уже выполняется!"), ephemeral=True)
            return
        await interaction.response.send_message(es("🔍 Запускаю проверку таблицы..."), ephemeral=True)
        await check_spreadsheet()
    
    # КНОПКА: извлечение структуры сообщения для анализа форматирования
    @discord.ui.button(label=es("🔍 Извлечь код сообщения"), style=discord.ButtonStyle.secondary, custom_id="admin_extract_message", row=3)
    async def extract_message_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        modal = ExtractMessageModal()
        await interaction.response.send_modal(modal)
    
    # КНОПКА: обновление шаблона для всех существующих мероприятий
    # Приводит все сообщения мероприятий к актуальному шаблону:
    # - добавляет картинки (автоопределение по названию)
    # - добавляет количество игр (AS VDV = 2, TT = 3)
    # - обновляет кнопки до актуального набора
    # Используйте после изменения шаблона embed-сообщения.
    @discord.ui.button(label=es("🔄 Обновить шаблон для мероприятий"), style=discord.ButtonStyle.primary, custom_id="admin_refresh_event_template", row=3)
    async def refresh_event_template_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        
        await interaction.response.send_message(
            es("🔄 Начинаю обновление шаблона для всех сообщений мероприятий...\n"
               "Это может занять несколько секунд."),
            ephemeral=True
        )
        
        updated_count, error_count = await update_all_event_messages()
        
        await interaction.followup.send(
            es(f"✅ Обновлено мероприятий: **{updated_count}**\n"
               f"⚠️ Ошибок: **{error_count}**"),
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
    
    @discord.ui.button(label=es("✅ Завершить отпуск досрочно"), style=discord.ButtonStyle.success, custom_id="vacation_end_early")
    async def end_early_button(self, interaction, button):
        nickname = self.get_nickname_by_message(interaction)
        if not nickname:
            await interaction.response.send_message(es("❌ Отпуск не найден!"), ephemeral=True)
            return
        if interaction.user.display_name != nickname and interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Только сам боец или командование!"), ephemeral=True)
            return
        await close_vacation(interaction, nickname, early=True, by_admin=False)
    
    @discord.ui.button(label=es("🔴 Закрыть отпуск (комбат)"), style=discord.ButtonStyle.danger, custom_id="vacation_admin_close")
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
    
    @discord.ui.button(label=es("📝 Заполнить явку"), style=discord.ButtonStyle.success, custom_id="event_attendance", row=1)
    async def attendance_button(self, interaction, button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        event_id = self.get_event_id_by_message(interaction)
        if not event_id:
            await interaction.response.send_message(es("❌ Мероприятие не найдено!"), ephemeral=True)
            return
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


# ============== МАСТЕР УЧЁТА ЯВКИ ==============

class AttendanceWizard:
    def __init__(self, event_id, num_games, event_title):
        self.event_id = event_id
        self.num_games = num_games
        self.event_title = event_title
        self.data = {}
        self.current_step = 0


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
            finish_btn = discord.ui.Button(label=es("✅ Отправить отчёт"), style=discord.ButtonStyle.success, custom_id=f"attendance_finish_{step}", row=4)
            finish_btn.callback = self.finish_callback
            self.add_item(finish_btn)
        elif wizard.num_games > 1 and step < wizard.num_games - 1:
            skip_btn = discord.ui.Button(label=es("⏭️ Пропустить эту игру"), style=discord.ButtonStyle.secondary, custom_id=f"attendance_skip_{step}", row=4)
            skip_btn.callback = self.skip_callback
            self.add_item(skip_btn)
            next_btn = discord.ui.Button(label=es(f"➡️ Сохранить и далее (Игра {step + 2})"), style=discord.ButtonStyle.primary, custom_id=f"attendance_next_{step}", row=4)
            next_btn.callback = self.next_callback
            self.add_item(next_btn)
        else:
            skip_btn = discord.ui.Button(label=es("⏭️ Пропустить эту игру"), style=discord.ButtonStyle.secondary, custom_id=f"attendance_skip_{step}", row=4)
            skip_btn.callback = self.skip_callback
            self.add_item(skip_btn)
            finish_btn = discord.ui.Button(label=es("✅ Отправить отчёт"), style=discord.ButtonStyle.success, custom_id=f"attendance_finish_{step}", row=4)
            finish_btn.callback = self.finish_callback
            self.add_item(finish_btn)
    
    def _make_select_callback(self, select):
        async def callback(interaction):
            if interaction.user.id not in ADMIN_USER_IDS:
                await interaction.response.send_message(es("⛔ Только комбат/заместитель!"), ephemeral=True)
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
        self.wizard.current_step = self.step + 1
        self.stop()
        await interaction.response.defer()
        await proceed_to_next_step(interaction, self.wizard)
    
    async def next_callback(self, interaction):
        if interaction.user.id not in ADMIN_USER_IDS:
            return
        selected = self._collect_all_selected()
        if self.wizard.num_games == 0:
            self.wizard.data["overall"] = selected
        else:
            self.wizard.data[self.step] = selected
        self.wizard.current_step = self.step + 1
        self.stop()
        await interaction.response.defer()
        await proceed_to_next_step(interaction, self.wizard)
    
    async def finish_callback(self, interaction):
        if interaction.user.id not in ADMIN_USER_IDS:
            return
        selected = self._collect_all_selected()
        if self.wizard.num_games == 0:
            self.wizard.data["overall"] = selected
        else:
            self.wizard.data[self.step] = selected
        self.stop()
        await interaction.response.defer()
        await finalize_attendance(interaction, self.wizard)


async def start_attendance_wizard(interaction, event_id):
    events = load_json(EVENTS_FILE, {})
    event = events.get(event_id)
    if not event:
        await interaction.response.send_message(es("❌ Мероприятие не найдено!"), ephemeral=True)
        return
    
    num_games = event.get('num_games', 0)
    wizard = AttendanceWizard(event_id, num_games, event.get('title', ''))
    clan_members = await load_clan_members_from_sheet()
    if not clan_members:
        await interaction.response.send_message(es("❌ Список клана пуст!"), ephemeral=True)
        return
    
    view = AttendanceStepView(wizard, 0, clan_members)
    if num_games == 0:
        title_text = es(f"👥 **{event.get('title', '')}**\n\n") + es("Выберите бойцов, явившихся на мероприятие:")
    else:
        title_text = es(f"👥 **{event.get('title', '')}**\n\n") + es(f"**Игра 1** из {num_games}\nВыберите явившихся:")
    
    await interaction.response.send_message(title_text, view=view, ephemeral=True)


async def proceed_to_next_step(interaction, wizard):
    clan_members = await load_clan_members_from_sheet()
    if wizard.num_games == 0 or wizard.current_step >= wizard.num_games:
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
    
    # ИСПРАВЛЕНИЕ 3: Удаляем старое сообщение явки, если оно есть
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
        'event_id': wizard.event_id,  # ИСПРАВЛЕНИЕ 6: Явная привязка
        'title': wizard.event_title,
        'date': event_start.strftime('%d.%m.%Y %H:%M'),
        'event_message_id': event.get('message_id'),
        'event_channel_id': event.get('channel_id'),
        'reported_at': datetime.now(MSK).isoformat(),
        'reported_by': interaction.user.display_name
    }
    
    if wizard.num_games == 0:
        record['overall'] = wizard.data.get('overall', [])
    else:
        record['games'] = {}
        for i in range(wizard.num_games):
            record['games'][str(i+1)] = wizard.data.get(i, [])
    
    thread = await get_or_create_thread(event, wizard.event_id, wizard.event_title)
    if not thread:
        await interaction.followup.send(es("❌ Не удалось получить ветку мероприятия!"), ephemeral=True)
        return
    
    # Сохраняем ID ветки в запись
    record['thread_id'] = thread.id
    
    # ИСПРАВЛЕНИЕ 1: Убираем ">>>" из отчёта
    report_text = es(f"🏆 **Отчёт о явке: {wizard.event_title}**\n\n")
    report_text += es(f"📋 Составил: **{interaction.user.display_name}**\n\n")
    
    if wizard.num_games == 0:
        players = wizard.data.get('overall', [])
        report_text += es(f"👥 **Явились на мероприятие ({len(players)}):**\n")
        if players:
            report_text += "\n".join(players)
        else:
            report_text += es("*Никто не явился*")
    else:
        for i in range(wizard.num_games):
            players = wizard.data.get(i, [])
            report_text += es(f"🎮 **Игра {i+1} ({len(players)} явилось):**\n")
            if players:
                report_text += "\n".join(players)
            else:
                report_text += es("*Никто не явился*")
            if i < wizard.num_games - 1:
                report_text += "\n\n"
    
    new_msg = await thread.send(report_text)
    
    # Сохраняем ID нового сообщения явки
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
        embed.add_field(name=es("📅 Период"), value=f"{start_str} - {end_str} ({duration} дней)", inline=True)
        embed.add_field(name=es("📝 Причина"), value=reason, inline=False)
        
        if by_admin:
            embed.add_field(name=es("👤 Оформил"), value=f"Комбат/заместитель: {interaction.user.display_name}", inline=False)
        else:
            embed.add_field(name=es("👤 Запросил"), value=interaction.user.display_name, inline=False)
        
        embed.add_field(name=es("ℹ️ Статус"), value=es("⏳ Ожидает утверждения комбатом"), inline=False)
        embed.set_footer(text="Комбат/заместитель: утвердите или отклоните отпуск")
        
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
                if field.name == es("ℹ️ Статус"):
                    embed.set_field_at(i, name=es("ℹ️ Статус"), value=es("✅ Утверждён и активен"), inline=False)
                    break
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
                                    break
                            embed.color = discord.Color.greyple()
                            await message.edit(embed=embed, view=None)
                    except Exception:
                        pass
        except Exception:
            pass
    
    if changed:
        save_json(VACATIONS_FILE, vacations)


# ============== ФУНКЦИИ МЕРОПРИЯТИЙ ==============

async def handle_event_response(interaction, event_id, response_type):
    events = load_json(EVENTS_FILE, {})
    if event_id not in events:
        await interaction.response.send_message(es("❌ Мероприятие не найдено!"), ephemeral=True)
        return
    
    event = events[event_id]
    nickname = interaction.user.display_name
    current_date = datetime.now(MSK)
    
    event_end = datetime.fromtimestamp(event['end_time'], MSK)
    if current_date > event_end:
        await interaction.response.send_message(es("⛔ Мероприятие уже завершено. Отметки больше не принимаются!"), ephemeral=True)
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
        event_id=event_id,
        current_title=event['title'],
        current_description=event['description'],
        current_start=start,
        current_end=end,
        image_key=image_key,
        num_games=event.get('num_games', 0)
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
    
    # ИСПРАВЛЕНИЕ 8: Удаляем ветку мероприятия
    if event.get('thread_id'):
        try:
            thread = await client.fetch_channel(event['thread_id'])
            await thread.delete()
            print(f"🗑️ Ветка мероприятия '{event.get('title')}' удалена")
        except Exception:
            pass
    
    # Удаляем основное сообщение
    try:
        channel = await client.fetch_channel(event['channel_id'])
        message = await channel.fetch_message(event['message_id'])
        await message.delete()
    except Exception:
        pass
    
    # ИСПРАВЛЕНИЕ 2: Удаляем явку из attendance_data.json
    attendance = load_json(ATTENDANCE_FILE, {})
    if event_id in attendance:
        del attendance[event_id]
        save_json(ATTENDANCE_FILE, attendance)
        print(f"🗑️ Явка для мероприятия '{event.get('title')}' удалена")
    
    del events[event_id]
    save_json(EVENTS_FILE, events)
    
    await interaction.response.send_message(es("✅ Мероприятие отменено!"), ephemeral=True)


async def build_event_embed(event_id):
    events = load_json(EVENTS_FILE, {})
    event = events[event_id]
    
    current_date = datetime.now(MSK)
    active_members = await get_active_members(current_date)
    
    accepted = list(event.get('accepted', {}).keys())
    declined = list(event.get('declined', {}).keys())
    unmarked = [m for m in active_members if m not in accepted and m not in declined]
    
    embed = discord.Embed(
        title=event['title'],
        description=event['description'],
        color=event.get('color', 15844367)
    )
    
    start_ts = int(event['start_time'])
    end_ts = int(event['end_time'])
    embed.add_field(name=es("⏰ Время"), value=f"<t:{start_ts}:F> - <t:{end_ts}:t>\n<t:{start_ts}:R>", inline=False)
    
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
    
    # ИСПРАВЛЕНИЕ 4: Новая формулировка
    num_games = event.get('num_games', 0)
    if num_games and num_games > 0:
        if num_games == 1:
            games_word = "игра"
        elif num_games in [2, 3, 4]:
            games_word = "игры"
        else:
            games_word = "игр"
        embed.add_field(name="", value=f"*На мероприятии запланировано {num_games} {games_word}*", inline=False)
    
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
        'title': title,
        'description': description,
        'start_time': int(start_time.timestamp()),
        'end_time': int(end_time.timestamp()),
        'accepted': {},
        'declined': {},
        'image_key': image_key,
        'num_games': num_games,
        'color': color,
        'channel_id': EVENTS_CHANNEL_ID,
        'message_id': None,
        'thread_id': None,
        'reminder_2days_sent': False,
        'reminder_15min_sent': False
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
        text += f"**{event['title']}**\n"
        text += f"ID: `{event_id}`\n"
        text += f"Дата: {start.strftime('%d.%m.%Y %H:%M')}\n"
        num_games = event.get('num_games', 0)
        if num_games and num_games > 0:
            text += f"🎮 Игр: {num_games}\n"
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
    
    await create_event(
        "СУББОТА - RTvT на AS VDV",
        f"Бойцы, на субботу запланировано мероприятие на сервере AS VDV. Ждём вас! Заходите в голосовой канал {voice_mention} за 15 минут до начала.",
        saturday_start, saturday_end, image_key='asvdv', num_games=2
    )
    
    await asyncio.sleep(2)
    
    await create_event(
        "ВОСКРЕСЕНЬЕ - TvT TT",
        f"Бойцы, на воскресенье запланировано мероприятие на сервере TT. Ждём вас! Заходите в голосовой канал {voice_mention} за 15 минут до начала.",
        sunday_start, sunday_end, image_key='tt', num_games=3
    )


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
                            reminder_text = (
                                es(f"📋 **Внимание: {event['title']}**\n\n") +
                                " ".join(mentions) + "\n\n" +
                                es("⏳ До мероприятия осталось **2 суток**, а вы ещё не отметились!\n\n") +
                                es("👉 Пожалуйста, отметьтесь в основном посте мероприятия.\n\n") +
                                f"Сбор в голосовом канале {voice_mention}."
                            )
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
                            reminder_text = (
                                es(f"🔔 **Бойцы, внимание: {event['title']}** 🔔\n\n") +
                                " ".join(mentions) + "\n\n" +
                                es(f"⚡ Мероприятие начнется через **{int(time_until_start.total_seconds() // 60)} минут**!\n\n") +
                                es("📍 Ждем всех на сборах! Заходите в голосовой канал:\n") +
                                es(f"👉 **{voice_mention}**")
                            )
                            await thread.send(reminder_text)
                    event['reminder_15min_sent'] = True
                    changed = True
        except Exception:
            pass
    
    if changed:
        save_json(EVENTS_FILE, events)

async def update_all_event_messages():
    """Обновляет шаблон всех существующих сообщений мероприятий,
    приводя их к актуальным требованиям:
    - добавляет картинки (автоопределение по названию)
    - добавляет количество игр (AS VDV = 2, TT = 3)
    - обновляет кнопки до актуального набора
    Используйте после изменения шаблона embed-сообщения."""
    events = load_json(EVENTS_FILE, {})
    updated_count = 0
    error_count = 0
    
    for event_id, event in events.items():
        title = event.get('title', '').lower()
        original_image = event.get('image_key', 'none')
        original_games = event.get('num_games', 0)
        
        # Определяем картинку по названию (если не установлена)
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
        
        # Определяем количество игр по умолчанию (если не установлено)
        num_games = original_games if original_games and original_games > 0 else 0
        if num_games == 0:
            if image_key == 'asvdv':
                num_games = 2
            elif image_key == 'tt':
                num_games = 3
        
        # Обновляем данные в JSON
        event['image_key'] = image_key
        event['num_games'] = num_games
        
        # Обновляем сообщение в Discord
        try:
            channel = await client.fetch_channel(event['channel_id'])
            message = await channel.fetch_message(event['message_id'])
            
            embed = await build_event_embed(event_id)
            
            filename, path = get_image_info(image_key)
            if filename and path:
                file = discord.File(path, filename=filename)
                await message.edit(embed=embed, attachments=[file], view=EventView())
            else:
                await message.edit(embed=embed, attachments=[], view=EventView())
            
            updated_count += 1
            print(f"✅ Обновлён шаблон '{event.get('title', '?')}' → картинка: {image_key}, игр: {num_games}")
        except discord.NotFound:
            print(f"⚠️ Сообщение мероприятия '{event.get('title', '?')}' не найдено (удалено)")
            error_count += 1
        except Exception as e:
            print(f"❌ Ошибка обновления '{event.get('title', '?')}': {e}")
            error_count += 1
    
    save_json(EVENTS_FILE, events)
    print(f"🔄 Итог обновления шаблона: обновлено {updated_count}, ошибок {error_count}")
    return updated_count, error_count

# ============== ПОСТОЯННАЯ ФУНКЦИЯ ИЗВЛЕЧЕНИЯ ==============

async def extract_message_structure(interaction, channel_id, message_id):
    """Извлекает полную структуру сообщения для анализа"""
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


# ============== СОБЫТИЯ DISCORD ==============

@client.event
async def on_ready():
    print(f'Бот запущен как {client.user} (PID {os.getpid()})')
    
    await load_clan_members_from_sheet()

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
            description=(
                "Здесь вы можете управлять всеми функциями бота через кнопки. "
                "Функции бота разделены по строчкам:\n\n"
                "1. Мероприятия \n"
                "2. Сообщения \n"
                "3. Отпуска \n"
                "4. Утилиты \n"
            ),
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
        if os.path.exists(LOCK_FILE):
            try:
                with open(LOCK_FILE, 'r') as f:
                    saved_pid = f.read().strip()
                if saved_pid == str(os.getpid()):
                    os.remove(LOCK_FILE)
            except Exception:
                pass