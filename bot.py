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
            print(f"❌ Бот уже запущен (PID {old_pid}). Останавливаю этот процесс, чтобы избежать дублирования.")
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
        '📢 ': '📢ㅤ', '⏳ ': '⏳ㅤ', '🔴 ': '🔴ㅤ',
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

# Кэш списка участников клана (загружается из таблицы)
CLAN_MEMBERS_CACHE = []
CLAN_MEMBERS_CACHE_TIME = None
CLAN_MEMBERS_CACHE_TTL = 3600  # 1 час

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
    """Загружает список участников клана из Google Таблицы с кэшированием"""
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
    """Ищет участника сервера по нику"""
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
        print(f"📤 Отправка сообщения для {user_name}: {len(text)} символов")
        await thread.send(text)
        await asyncio.sleep(0.5)
        return

    chunks = []
    for i in range(0, len(text), chunk_size):
        chunk = text[i:i + chunk_size]
        chunks.append(chunk)

    print(f"📤 Отправка сообщения для {user_name}: {len(text)} символов → {len(chunks)} частей")

    for i, chunk in enumerate(chunks, 1):
        print(f"  → Часть {i}/{len(chunks)}: {len(chunk)} символов")
        try:
            await thread.send(chunk)
            await asyncio.sleep(0.5)
        except Exception as e:
            print(f"  ❌ Ошибка при отправке части {i}: {e}")
            raise


# ============== ПОСТРОЕНИЕ ТЕКСТОВ СООБЩЕНИЙ ==============

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
    lines = build_intro_lines(current_time)
    return "\n".join(lines)


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
        parts.append(
            es("🟡 **Важные, но менее критические проблемы, ") +
            "также требующие своевременного исправления:**"
        )
        for issue in yellow_issues:
            parts.append(issue_line(issue))
        parts.append("")

    parts.append("─" * 50)

    return "\n".join(parts).strip("\n")


async def check_spreadsheet():
    if check_lock.locked():
        print(es("⚠️ Проверка уже выполняется, пропускаем."))
        return

    async with check_lock:
        print(f"🔍 Начинаем проверку таблицы в {datetime.now(MSK).strftime('%H:%M:%S')}")

        try:
            if not gc:
                print("Google Sheets не инициализирован")
                return

            spreadsheet = gc.open_by_url(SPREADSHEET_URL)
            sheet = spreadsheet.worksheet(SHEET_NAME)

            data_with_colors = get_sheet_data_with_colors(sheet, 'A1:J28')

            if not data_with_colors or len(data_with_colors) < 2:
                print("Таблица пуста")
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

                # Проверяем только утверждённые отпуска из JSON
                if is_on_vacation_dynamic(nickname, current_time):
                    print(f"✅ {nickname} в отпуске, пропускаем проверку.")
                    continue

                issues = []
                for col_name in COLUMNS_TO_CHECK:
                    if col_name in headers:
                        col_idx = headers.index(col_name)
                        if col_idx < len(row):
                            cell_data = row[col_idx]
                            color = get_color_category(cell_data['bg'])

                            if color in ['red', 'yellow']:
                                issues.append({
                                    'column': col_name,
                                    'text': cell_data['value'].strip(),
                                    'severity': color
                                })

                if issues:
                    discord_user = await find_discord_user(nickname, thread)
                    if discord_user:
                        user_issues[discord_user] = issues
                    else:
                        users_not_found.append(nickname)

            if user_issues or users_not_found:
                intro = build_intro_message(current_time)

                if len(intro) > EXPECTED_INTRO_MAX_LEN:
                    print(f"⚠️ ВНИМАНИЕ: intro подозрительно длинный ({len(intro)} символов, "
                          f"ожидалось не более {EXPECTED_INTRO_MAX_LEN}). "
                          f"Похоже на дублирование текста в коде! Отправка intro отменена.")
                else:
                    await send_chunked(thread, intro, "вводное сообщение")

                for discord_user, issues in user_issues.items():
                    user_msg = build_user_message(discord_user, issues)
                    await send_chunked(thread, user_msg, discord_user.display_name)

                if users_not_found:
                    not_found_msg = (
                        "─" * 50 + "\n\n"
                        + es("⚠️ **Не удалось найти в Discord:**\n")
                        + ", ".join(users_not_found)
                    )
                    await send_chunked(thread, not_found_msg, "список ненайденных")

                print(f"✅ Проверка завершена. Отправлено уведомлений: {len(user_issues)}")
            else:
                print("✅ Проблем не обнаружено")

        except Exception as e:
            print(f"Ошибка при проверке: {e}")
            try:
                thread = await client.fetch_channel(THREAD_ID)
                await thread.send(f"❌ Ошибка при проверке таблицы: {e}")
            except Exception:
                pass

# ============== НОВЫЙ ФУНКЦИОНАЛ: РАБОТА С ДАННЫМИ ==============

def load_json(filename, default=None):
    if default is None:
        default = {}
    if not os.path.exists(filename):
        return default
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        print(f"Ошибка загрузки {filename}: {e}")
        return default


def save_json(filename, data):
    try:
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Ошибка сохранения {filename}: {e}")


def is_on_vacation_dynamic(nickname: str, current_date: datetime) -> bool:
    """Проверяет только УТВЕРЖДЁННЫЕ отпуска из JSON (статус 'active')"""
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
    """Возвращает список активных участников клана (из таблицы, исключая отпускников)"""
    members = await load_clan_members_from_sheet()
    return [m for m in members if not is_on_vacation_dynamic(m, current_date)]


async def get_vacation_role(guild):
    role = discord.utils.get(guild.roles, name="Отпуск")
    if not role:
        try:
            role = await guild.create_role(
                name="Отпуск",
                reason="Автоматическое создание роли для отпусков",
                mentionable=False
            )
            print(f"✅ Создана роль 'Отпуск' (ID: {role.id})")
        except Exception as e:
            print(f"❌ Ошибка создания роли: {e}")
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
                await member.add_roles(role, reason="Утверждение отпуска")
                print(f"✅ Роль 'Отпуск' добавлена {member.display_name}")
        else:
            if role in member.roles:
                await member.remove_roles(role, reason="Завершение отпуска")
                print(f"✅ Роль 'Отпуск' снята с {member.display_name}")
    except Exception as e:
        print(f"❌ Ошибка обновления роли: {e}")


# ============== UI КОМПОНЕНТЫ ==============

class VacationModal(discord.ui.Modal, title=es("🏖️ Оформление отпуска")):
    start_date = discord.ui.TextInput(
        label="Дата начала (ДД.ММ.ГГГГ)",
        placeholder="15.08.2026",
        required=True,
        max_length=10
    )
    end_date = discord.ui.TextInput(
        label="Дата окончания (ДД.ММ.ГГГГ)",
        placeholder="22.08.2026",
        required=True,
        max_length=10
    )
    reason = discord.ui.TextInput(
        label="Причина отпуска",
        placeholder="Командировка, семейные обстоятельства и т.д.",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=500
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        await handle_vacation_request(
            interaction, 
            interaction.user.display_name,
            self.start_date.value, 
            self.end_date.value,
            self.reason.value,
            by_admin=False
        )


class AdminVacationModal(discord.ui.Modal, title=es("🏖️ Отпуск для бойца (комбат)")):
    player_name = discord.ui.TextInput(
        label="Позывной бойца с клантегом (как в Discord)",
        placeholder="[En-Y]Killa",
        required=True,
        max_length=35
    )
    start_date = discord.ui.TextInput(
        label="Дата начала (ДД.ММ.ГГГГ)",
        placeholder="15.08.2026",
        required=True,
        max_length=10
    )
    end_date = discord.ui.TextInput(
        label="Дата окончания (ДД.ММ.ГГГГ)",
        placeholder="22.08.2026",
        required=True,
        max_length=10
    )
    reason = discord.ui.TextInput(
        label="Причина отпуска",
        placeholder="Командировка, семейные обстоятельства и т.д.",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=500
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        await handle_vacation_request(
            interaction,
            self.player_name.value,
            self.start_date.value,
            self.end_date.value,
            self.reason.value,
            by_admin=True
        )


class SendMessageModal(discord.ui.Modal, title=es("📝 Отправка сообщения")):
    channel_id = discord.ui.TextInput(
        label="ID канала или ветки",
        placeholder="123456789012345678",
        required=True,
        max_length=20
    )
    message_text = discord.ui.TextInput(
        label="Текст сообщения",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=2000
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            channel_id = int(self.channel_id.value)
            channel = await client.fetch_channel(channel_id)
            await channel.send(self.message_text.value)
            await interaction.response.send_message(es("✅ Сообщение отправлено!"), ephemeral=True)
        except ValueError:
            await interaction.response.send_message(es("❌ Неверный ID канала!"), ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)


class DeleteMessageModal(discord.ui.Modal, title=es("🗑️ Удаление сообщения")):
    channel_id = discord.ui.TextInput(
        label="ID канала или ветки",
        placeholder="123456789012345678",
        required=True,
        max_length=20
    )
    message_id = discord.ui.TextInput(
        label="ID сообщения",
        placeholder="123456789012345678",
        required=True,
        max_length=20
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            channel_id = int(self.channel_id.value)
            message_id = int(self.message_id.value)
            channel = await client.fetch_channel(channel_id)
            message = await channel.fetch_message(message_id)
            await message.delete()
            await interaction.response.send_message(es("✅ Сообщение удалено!"), ephemeral=True)
        except ValueError:
            await interaction.response.send_message(es("❌ Неверный ID!"), ephemeral=True)
        except discord.NotFound:
            await interaction.response.send_message(es("❌ Сообщение не найдено!"), ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)


class EventCreateModal(discord.ui.Modal, title=es("📅 Создание мероприятия")):
    event_title = discord.ui.TextInput(
        label="Название мероприятия",
        placeholder="СУББОТА - RTvT AS VDV",
        required=True,
        max_length=100
    )
    event_description = discord.ui.TextInput(
        label="Описание",
        style=discord.TextStyle.paragraph,
        placeholder="Описание мероприятия...",
        required=True,
        max_length=1000
    )
    start_time = discord.ui.TextInput(
        label="Начало (ДД.ММ.ГГГГ ЧЧ:ММ)",
        placeholder="15.08.2026 16:30",
        required=True,
        max_length=16
    )
    end_time = discord.ui.TextInput(
        label="Окончание (ДД.ММ.ГГГГ ЧЧ:ММ)",
        placeholder="15.08.2026 19:30",
        required=True,
        max_length=16
    )
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            start = datetime.strptime(self.start_time.value, "%d.%m.%Y %H:%M")
            end = datetime.strptime(self.end_time.value, "%d.%m.%Y %H:%M")
            start = MSK.localize(start)
            end = MSK.localize(end)
            
            await create_event(
                self.event_title.value,
                self.event_description.value,
                start,
                end
            )
            await interaction.response.send_message(es("✅ Мероприятие создано!"), ephemeral=True)
        except ValueError:
            await interaction.response.send_message(es("❌ Неверный формат даты! Используйте ДД.ММ.ГГГГ ЧЧ:ММ"), ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)


class EventEditModal(discord.ui.Modal, title=es("✏️ Редактирование мероприятия")):
    def __init__(self, event_id: str, current_title: str, current_description: str, current_start: str, current_end: str):
        super().__init__(title=es("✏️ Редактирование мероприятия"))
        self.event_id = event_id
        
        self.event_title = discord.ui.TextInput(
            label="Название мероприятия",
            default=current_title,
            required=True,
            max_length=100
        )
        self.event_description = discord.ui.TextInput(
            label="Описание",
            style=discord.TextStyle.paragraph,
            default=current_description,
            required=True,
            max_length=1000
        )
        self.start_time = discord.ui.TextInput(
            label="Начало (ДД.ММ.ГГГГ ЧЧ:ММ)",
            default=current_start,
            required=True,
            max_length=16
        )
        self.end_time = discord.ui.TextInput(
            label="Окончание (ДД.ММ.ГГГГ ЧЧ:ММ)",
            default=current_end,
            required=True,
            max_length=16
        )
        
        self.add_item(self.event_title)
        self.add_item(self.event_description)
        self.add_item(self.start_time)
        self.add_item(self.end_time)
    
    async def on_submit(self, interaction: discord.Interaction):
        try:
            start = datetime.strptime(self.start_time.value, "%d.%m.%Y %H:%M")
            end = datetime.strptime(self.end_time.value, "%d.%m.%Y %H:%M")
            start = MSK.localize(start)
            end = MSK.localize(end)
            
            await update_event(
                self.event_id,
                self.event_title.value,
                self.event_description.value,
                start,
                end
            )
            await interaction.response.send_message(es("✅ Мероприятие обновлено!"), ephemeral=True)
        except ValueError:
            await interaction.response.send_message(es("❌ Неверный формат даты! Используйте ДД.ММ.ГГГГ ЧЧ:ММ"), ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)


class AdminMainMenuView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    # ═══════════ РЯД 1: МЕРОПРИЯТИЯ ═══════════
    
    @discord.ui.button(label=es("📅 Создать мероприятие"), style=discord.ButtonStyle.primary, custom_id="admin_create_event", row=0)
    async def create_event_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        modal = EventCreateModal()
        await interaction.response.send_modal(modal)
    
    # === КНОПКА СКРЫТА (закомментирована) ===
    # @discord.ui.button(label=es("📅 Мероприятия недели"), style=discord.ButtonStyle.primary, custom_id="admin_force_weekly", row=0)
    # async def force_weekly_button(self, interaction: discord.Interaction, button: discord.ui.Button):
    #     if interaction.user.id not in ADMIN_USER_IDS:
    #         await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
    #         return
    #     await interaction.response.defer(ephemeral=True)
    #     await post_weekly_events()
    #     await interaction.followup.send(es("✅ Мероприятия на эту неделю опубликованы!"), ephemeral=True)
    # === КОНЕЦ СКРЫТОЙ КНОПКИ ===
    
    @discord.ui.button(label=es("📋 Список мероприятий"), style=discord.ButtonStyle.secondary, custom_id="admin_event_list", row=0)
    async def event_list_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        await show_event_list(interaction)
    
    # ═══════════ РЯД 2: СООБЩЕНИЯ ═══════════
    
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
    
    # ═══════════ РЯД 3: ОТПУСКА ═══════════
    
    @discord.ui.button(label=es("🏖️ Отпуск для бойца"), style=discord.ButtonStyle.primary, custom_id="admin_vacation_for_player", row=2)
    async def vacation_for_player_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        modal = AdminVacationModal()
        await interaction.response.send_modal(modal)
    
    # === КНОПКА СКРЫТА (закомментирована) ===
    # @discord.ui.button(label=es("📋 Правила отпусков"), style=discord.ButtonStyle.secondary, custom_id="admin_vacation_rules", row=2)
    # async def vacation_rules_button(self, interaction: discord.Interaction, button: discord.ui.Button):
    #     if interaction.user.id not in ADMIN_USER_IDS:
    #         await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
    #         return
    #     await publish_vacation_info(interaction)
    # === КОНЕЦ СКРЫТОЙ КНОПКИ ===
    
    @discord.ui.button(label=es("🏖️ Список отпусков"), style=discord.ButtonStyle.secondary, custom_id="admin_vacation_list", row=2)
    async def vacation_list_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        await show_vacation_list(interaction)
    
    # ═══════════ РЯД 4: УТИЛИТЫ ═══════════
    
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


class VacationRequestView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(label=es("🏖️ Оформить отпуск"), style=discord.ButtonStyle.primary, custom_id="vacation_request")
    async def vacation_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = VacationModal()
        await interaction.response.send_modal(modal)


class VacationApprovalView(discord.ui.View):
    """Кнопки для утверждения отпуска комбатом.
    Ищет nickname по message_id из JSON, чтобы работать после перезапуска бота."""
    def __init__(self):
        super().__init__(timeout=None)
    
    def get_nickname_by_message(self, interaction):
        """Находит nickname по message_id из vacations.json"""
        vacations = load_json(VACATIONS_FILE, {})
        message_id = interaction.message.id
        for nickname, data in vacations.items():
            if data.get('message_id') == message_id:
                return nickname
        return None
    
    @discord.ui.button(label=es("✅ Утвердить отпуск"), style=discord.ButtonStyle.success, custom_id="vacation_approve")
    async def approve_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        nickname = self.get_nickname_by_message(interaction)
        if not nickname:
            await interaction.response.send_message(es("❌ Отпуск не найден! Возможно, данные были удалены."), ephemeral=True)
            return
        await approve_vacation(interaction, nickname)
    
    @discord.ui.button(label=es("❌ Отклонить отпуск"), style=discord.ButtonStyle.danger, custom_id="vacation_reject")
    async def reject_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        nickname = self.get_nickname_by_message(interaction)
        if not nickname:
            await interaction.response.send_message(es("❌ Отпуск не найден! Возможно, данные были удалены."), ephemeral=True)
            return
        await reject_vacation(interaction, nickname)


class VacationMessageView(discord.ui.View):
    """Кнопки для утверждённого отпуска.
    Ищет nickname по message_id из JSON, чтобы работать после перезапуска бота."""
    def __init__(self):
        super().__init__(timeout=None)
    
    def get_nickname_by_message(self, interaction):
        """Находит nickname по message_id из vacations.json"""
        vacations = load_json(VACATIONS_FILE, {})
        message_id = interaction.message.id
        for nickname, data in vacations.items():
            if data.get('message_id') == message_id:
                return nickname
        return None
    
    @discord.ui.button(label=es("✅ Завершить отпуск досрочно"), style=discord.ButtonStyle.success, custom_id="vacation_end_early")
    async def end_early_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        nickname = self.get_nickname_by_message(interaction)
        if not nickname:
            await interaction.response.send_message(es("❌ Отпуск не найден! Возможно, данные были удалены."), ephemeral=True)
            return
        if interaction.user.display_name != nickname and interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Только сам боец или командование может закрыть отпуск!"), ephemeral=True)
            return
        await close_vacation(interaction, nickname, early=True, by_admin=False)
    
    @discord.ui.button(label=es("🔴 Закрыть отпуск (комбат)"), style=discord.ButtonStyle.danger, custom_id="vacation_admin_close")
    async def admin_close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        nickname = self.get_nickname_by_message(interaction)
        if not nickname:
            await interaction.response.send_message(es("❌ Отпуск не найден! Возможно, данные были удалены."), ephemeral=True)
            return
        await close_vacation(interaction, nickname, early=True, by_admin=True)


class EventView(discord.ui.View):
    """Кнопки для мероприятия.
    Ищет event_id по message_id из JSON, чтобы работать после перезапуска бота."""
    def __init__(self):
        super().__init__(timeout=None)
    
    def get_event_id_by_message(self, interaction):
        """Находит event_id по message_id из events.json"""
        events = load_json(EVENTS_FILE, {})
        message_id = interaction.message.id
        for event_id, event in events.items():
            if event.get('message_id') == message_id:
                return event_id
        return None
    
    @discord.ui.button(label=es("✅ Приду"), style=discord.ButtonStyle.success, custom_id="event_accept", row=0)
    async def accept_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        event_id = self.get_event_id_by_message(interaction)
        if not event_id:
            await interaction.response.send_message(es("❌ Мероприятие не найдено! Возможно, оно было удалено."), ephemeral=True)
            return
        await handle_event_response(interaction, event_id, "accept")
    
    @discord.ui.button(label=es("❌ Не приду"), style=discord.ButtonStyle.danger, custom_id="event_decline", row=0)
    async def decline_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        event_id = self.get_event_id_by_message(interaction)
        if not event_id:
            await interaction.response.send_message(es("❌ Мероприятие не найдено! Возможно, оно было удалено."), ephemeral=True)
            return
        await handle_event_response(interaction, event_id, "decline")
    
    @discord.ui.button(label=es("🔄 Обновить список"), style=discord.ButtonStyle.primary, custom_id="event_refresh", row=1)
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        event_id = self.get_event_id_by_message(interaction)
        if not event_id:
            await interaction.response.send_message(es("❌ Мероприятие не найдено! Возможно, оно было удалено."), ephemeral=True)
            return
        await refresh_event_message(interaction, event_id)
    
    @discord.ui.button(label=es("✏️ Редактировать"), style=discord.ButtonStyle.secondary, custom_id="event_edit", row=1)
    async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        event_id = self.get_event_id_by_message(interaction)
        if not event_id:
            await interaction.response.send_message(es("❌ Мероприятие не найдено! Возможно, оно было удалено."), ephemeral=True)
            return
        await open_edit_modal(interaction, event_id)
    
    @discord.ui.button(label=es("❌ Отменить мероприятие"), style=discord.ButtonStyle.danger, custom_id="event_cancel", row=1)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message(es("⛔ Доступно только комбату и его заместителям!"), ephemeral=True)
            return
        event_id = self.get_event_id_by_message(interaction)
        if not event_id:
            await interaction.response.send_message(es("❌ Мероприятие не найдено! Возможно, оно было удалено."), ephemeral=True)
            return
        await cancel_event(interaction, event_id)


# ============== ФУНКЦИИ ОТПУСКОВ ==============

async def publish_vacation_info(interaction: discord.Interaction):
    try:
        channel = await client.fetch_channel(VACATION_CHANNEL_ID)
        
        embed = discord.Embed(
            title=es("🏖️ Оформление отпусков"),
            description=VACATION_RULES,
            color=discord.Color.green()
        )
        embed.set_footer(text="Нажмите кнопку ниже, чтобы оформить отпуск")
        
        view = VacationRequestView()
        await channel.send(embed=embed, view=view)
        
        await interaction.response.send_message(es("✅ Правила отпусков опубликованы!"), ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)


async def handle_vacation_request(interaction: discord.Interaction, nickname: str, start_str: str, end_str: str, reason: str, by_admin: bool):
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
        
        # Проверка даты только для обычных бойцов, НЕ для комбата/заместителей
        if not by_admin:
            if start_date.date() < datetime.now(MSK).date():
                await interaction.response.send_message(es("❌ Дата начала должна быть в будущем!"), ephemeral=True)
                return
        
        member = await find_member_by_nickname(nickname)
        if not member:
            await interaction.response.send_message(f"❌ Боец {nickname} не найден на сервере!", ephemeral=True)
            return
        
        vacations = load_json(VACATIONS_FILE, {})
        
        if nickname in vacations:
            current_status = vacations[nickname].get('status')
            if current_status in ['active', 'pending']:
                await interaction.response.send_message(f"⚠️ У {nickname} уже есть отпуск в статусе '{current_status}'!", ephemeral=True)
                return
        
        # Сохраняем флаг by_admin для правильного отображения при утверждении
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
        
        # Формируем правильное описание в зависимости от того, кто оформляет
        if by_admin:
            embed_description = f"Отпуск для **{nickname}**"
        else:
            embed_description = f"**{nickname}** запросил(а) отпуск"
        
        embed = discord.Embed(
            title=es("🏖️ Отпуск требует утверждения"),
            description=embed_description,
            color=discord.Color.orange()
        )
        embed.add_field(name=es("📅 Период"), value=f"{start_str} - {end_str} ({duration} дней)", inline=True)
        embed.add_field(name=es("📝 Причина"), value=reason, inline=False)
        
        # Правильное поле в зависимости от того, кто оформил
        if by_admin:
            embed.add_field(name=es("👤 Оформил"), value=f"Комбат/заместитель: {interaction.user.display_name}", inline=False)
        else:
            embed.add_field(name=es("👤 Запросил"), value=interaction.user.display_name, inline=False)
        
        embed.add_field(name=es("ℹ️ Статус"), value=es("⏳ Ожидает утверждения комбатом"), inline=False)
        embed.set_footer(text="Комбат/заместитель: утвердите или отклоните отпуск")
        
        # View БЕЗ аргументов — будет искать nickname по message_id
        view = VacationApprovalView()
        message = await channel.send(embed=embed, view=view)
        
        vacations[nickname]['message_id'] = message.id
        vacations[nickname]['channel_id'] = channel.id
        
        # === СОЗДАЁМ ВЕТКУ И УВЕДОМЛЯЕМ КОМБАТА/ЗАМОВ ===
        try:
            thread = await message.create_thread(name=f"💬 Утверждение отпуска - {nickname}")
            guild = channel.guild
            
            # Ищем роли "Комбат ArmA" и "Зам. комбата ArmA"
            mentions = []
            role_kombat = discord.utils.get(guild.roles, name="Комбат ArmA")
            role_zam = discord.utils.get(guild.roles, name="Зам. комбата ArmA")
            
            if role_kombat:
                mentions.append(role_kombat.mention)
            if role_zam:
                mentions.append(role_zam.mention)
            
            # Ссылка на канал с отпусками (по аналогии с voice_mention)
            vacation_mention = f"<#{VACATION_CHANNEL_ID}>"
            
            if mentions:
                mentions_text = " ".join(mentions)
                await thread.send(
                    f"{mentions_text}\n\n" +
                    es(f"📋 Появился новый запрос на отпуск от **{nickname}** на утверждение!\n\n") +
                    es(f"👉 Пожалуйста, перейдите в канал {vacation_mention} и рассмотрите рапорт.")
                )
                print(f"✅ Уведомление об отпуске {nickname} отправлено в ветку (ролей: {len(mentions)})")
            else:
                # Если роли не найдены, отправим уведомление без пинга
                await thread.send(
                    es(f"📋 Появился новый запрос на отпуск от **{nickname}** на утверждение!\n\n") +
                    es(f"👉 Пожалуйста, перейдите в канал {vacation_mention} и рассмотрите рапорт.")
                )
                print(f"⚠️ Роли комбата не найдены, уведомление отправлено без пинга")
            
            # Сохраняем ID ветки
            vacations[nickname]['thread_id'] = thread.id
            
        except Exception as e:
            print(f"❌ Ошибка создания ветки для отпуска: {e}")
        
        # Финальное сохранение с thread_id
        save_json(VACATIONS_FILE, vacations)
        
        await interaction.response.send_message(
            es("✅ Запрос на отпуск отправлен!\n") +
            "Отпуск будет действовать после утверждения комбатом или заместителем.",
            ephemeral=True
        )
        
        print(f"🏖️ {nickname}: отпуск с {start_str} по {end_str}. Причина: {reason}. Комбат/заместитель: {by_admin}")
        
    except ValueError:
        await interaction.response.send_message(
            es("❌ Неверный формат даты! Используйте ДД.ММ.ГГГГ (например, 15.08.2026)"),
            ephemeral=True
        )
    except Exception as e:
        print(f"Ошибка оформления отпуска: {e}")
        await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)


async def approve_vacation(interaction: discord.Interaction, nickname: str):
    vacations = load_json(VACATIONS_FILE, {})
    
    if nickname not in vacations:
        await interaction.response.send_message(es("❌ Отпуск не найден!"), ephemeral=True)
        return
    
    vacation = vacations[nickname]
    if vacation.get('status') != 'pending':
        await interaction.response.send_message(es("⚠️ Этот отпуск уже обработан!"), ephemeral=True)
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
            
            # Исправляем описание в зависимости от того, кто оформлял
            if vacation.get('by_admin', False):
                embed.description = f"Отпуск для **{nickname}**"
            else:
                embed.description = f"**{nickname}** взял(а) отпуск"
            
            for i, field in enumerate(embed.fields):
                if field.name == es("ℹ️ Статус"):
                    embed.set_field_at(i, name=es("ℹ️ Статус"), value=es("✅ Утверждён и активен"), inline=False)
                    break
            embed.add_field(name=es("✅ Утвердил"), value=interaction.user.display_name, inline=False)
            embed.set_footer(text="Во время отпуска вам не нужно отмечаться в расписании мероприятий")
            
            # View БЕЗ аргументов — будет искать nickname по message_id
            new_view = VacationMessageView()
            await message.edit(embed=embed, view=new_view)
    except Exception as e:
        print(f"Ошибка обновления сообщения: {e}")
    
    await interaction.response.send_message(
        f"✅ Отпуск {nickname} утверждён! Роль 'Отпуск' добавлена.",
        ephemeral=True
    )
    print(f"✅ Отпуск {nickname} утверждён {interaction.user.display_name}")


async def reject_vacation(interaction: discord.Interaction, nickname: str):
    vacations = load_json(VACATIONS_FILE, {})
    
    if nickname not in vacations:
        await interaction.response.send_message(es("❌ Отпуск не найден!"), ephemeral=True)
        return
    
    vacation = vacations[nickname]
    if vacation.get('status') != 'pending':
        await interaction.response.send_message(es("⚠️ Этот отпуск уже обработан!"), ephemeral=True)
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
            
            # Исправляем описание в зависимости от того, кто оформлял
            if vacation.get('by_admin', False):
                embed.description = f"Отпуск для **{nickname}** отклонён"
            else:
                embed.description = f"Запрос на отпуск **{nickname}** отклонён"
            
            for i, field in enumerate(embed.fields):
                if field.name == es("ℹ️ Статус"):
                    embed.set_field_at(i, name=es("ℹ️ Статус"), value=es("❌ Отклонён командованием"), inline=False)
                    break
            embed.add_field(name=es("❌ Отклонил"), value=interaction.user.display_name, inline=False)
            embed.set_footer(text="Отпуск аннулирован. Боец должен отмечаться на мероприятия.")
            
            await message.edit(embed=embed, view=None)
    except Exception as e:
        print(f"Ошибка обновления сообщения: {e}")
    
    await interaction.response.send_message(
        f"❌ Отпуск {nickname} отклонён.",
        ephemeral=True
    )
    print(f"❌ Отпуск {nickname} отклонён {interaction.user.display_name}")


async def close_vacation(interaction: discord.Interaction, nickname: str, early: bool = False, by_admin: bool = False):
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
    except Exception as e:
        print(f"Ошибка обновления сообщения: {e}")
    
    who_closed = "комбат/заместитель" if by_admin else "боец"
    await interaction.response.send_message(
        f"✅ Отпуск {nickname} закрыт ({who_closed}). Роль 'Отпуск' снята.",
        ephemeral=True
    )
    print(f"✅ Отпуск {nickname} закрыт. Ранний: {early}, Комбат/заместитель: {by_admin}")


async def show_vacation_list(interaction: discord.Interaction):
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
            text += f"Причина: {data.get('reason', 'Не указана')}\n"
            created_by = data.get('created_by', 'Сам боец')
            if data.get('by_admin', False):
                text += f"Оформил комбат/заместитель: {created_by}\n\n"
            else:
                text += f"Запросил: {created_by}\n\n"
    
    if active:
        text += es("✅ **Активные отпуска:**\n\n")
        for nickname, data in active.items():
            start = datetime.fromisoformat(data['start']).strftime('%d.%m.%Y')
            end = datetime.fromisoformat(data['end']).strftime('%d.%m.%Y')
            text += f"**{nickname}**: {start} - {end}\n"
            text += f"Причина: {data.get('reason', 'Не указана')}\n"
            text += f"Утвердил: {data.get('approved_by', 'Неизвестно')}\n\n"
    
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
                
                try:
                    member = await find_member_by_nickname(nickname)
                    if member:
                        await update_vacation_role(member, False)
                except Exception:
                    pass
                
                try:
                    if data.get('message_id') and data.get('channel_id'):
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
                
                print(f"✅ Отпуск {nickname} автоматически закрыт (истек срок)")
        except Exception as e:
            print(f"Ошибка проверки отпуска {nickname}: {e}")
    
    if changed:
        save_json(VACATIONS_FILE, vacations)


# ============== ФУНКЦИИ МЕРОПРИЯТИЙ ==============

async def handle_event_response(interaction: discord.Interaction, event_id: str, response_type: str):
    events = load_json(EVENTS_FILE, {})
    if event_id not in events:
        await interaction.response.send_message(es("❌ Мероприятие не найдено!"), ephemeral=True)
        return
    
    event = events[event_id]
    nickname = interaction.user.display_name
    current_date = datetime.now(MSK)
    
    if is_on_vacation_dynamic(nickname, current_date):
        await interaction.response.send_message(
            es("🏖️ Вы сейчас в отпуске. Во время отпуска вам не нужно отмечаться в расписании мероприятий."),
            ephemeral=True
        )
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
        await message.edit(embed=embed)
    except Exception as e:
        print(f"Ошибка обновления сообщения: {e}")


async def refresh_event_message(interaction: discord.Interaction, event_id: str):
    events = load_json(EVENTS_FILE, {})
    if event_id not in events:
        await interaction.response.send_message(es("❌ Мероприятие не найдено!"), ephemeral=True)
        return
    
    try:
        event = events[event_id]
        channel = await client.fetch_channel(event['channel_id'])
        message = await channel.fetch_message(event['message_id'])
        embed = await build_event_embed(event_id)
        await message.edit(embed=embed)
        await interaction.response.send_message(es("✅ Список участников обновлен!"), ephemeral=True)
    except Exception as e:
        print(f"Ошибка обновления: {e}")
        await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)


async def open_edit_modal(interaction: discord.Interaction, event_id: str):
    """Открывает модальное окно для редактирования мероприятия"""
    events = load_json(EVENTS_FILE, {})
    if event_id not in events:
        await interaction.response.send_message(es("❌ Мероприятие не найдено!"), ephemeral=True)
        return
    
    event = events[event_id]
    start = datetime.fromtimestamp(event['start_time'], MSK).strftime("%d.%m.%Y %H:%M")
    end = datetime.fromtimestamp(event['end_time'], MSK).strftime("%d.%m.%Y %H:%M")
    
    modal = EventEditModal(
        event_id=event_id,
        current_title=event['title'],
        current_description=event['description'],
        current_start=start,
        current_end=end
    )
    await interaction.response.send_modal(modal)


async def update_event(event_id: str, title: str, description: str, start_time: datetime, end_time: datetime):
    """Обновляет существующее мероприятие"""
    events = load_json(EVENTS_FILE, {})
    if event_id not in events:
        return
    
    event = events[event_id]
    event['title'] = title
    event['description'] = description
    event['start_time'] = int(start_time.timestamp())
    event['end_time'] = int(end_time.timestamp())
    # Сбрасываем флаги напоминаний, чтобы они отправились заново для новых дат
    event['reminder_2days_sent'] = False
    event['reminder_15min_sent'] = False
    save_json(EVENTS_FILE, events)
    
    try:
        channel = await client.fetch_channel(event['channel_id'])
        message = await channel.fetch_message(event['message_id'])
        embed = await build_event_embed(event_id)
        await message.edit(embed=embed)
        print(f"✅ Мероприятие '{title}' обновлено")
    except Exception as e:
        print(f"❌ Ошибка обновления мероприятия: {e}")


async def cancel_event(interaction: discord.Interaction, event_id: str):
    events = load_json(EVENTS_FILE, {})
    if event_id in events:
        event = events[event_id]
        try:
            channel = await client.fetch_channel(event['channel_id'])
            message = await channel.fetch_message(event['message_id'])
            await message.delete()
        except Exception:
            pass
        
        del events[event_id]
        save_json(EVENTS_FILE, events)
        await interaction.response.send_message(es("✅ Мероприятие отменено!"), ephemeral=True)
    else:
        await interaction.response.send_message(es("❌ Мероприятие не найдено!"), ephemeral=True)


async def build_event_embed(event_id: str) -> discord.Embed:
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
    embed.add_field(
        name=es("⏰ Время"),
        value=f"<t:{start_ts}:F> - <t:{end_ts}:t>\n<t:{start_ts}:R>",
        inline=False
    )
    
    if accepted:
        embed.add_field(
            name=es(f"✅ Придут ({len(accepted)})"),
            value=">>> " + "\n".join(accepted),
            inline=True
        )
    if declined:
        embed.add_field(
            name=es(f"❌ Не придут ({len(declined)})"),
            value=">>> " + "\n".join(declined),
            inline=True
        )
    if unmarked:
        embed.add_field(
            name=es(f"❓ Не отметились ({len(unmarked)})"),
            value=">>> " + "\n".join(unmarked),
            inline=False
        )
    
    if event.get('image'):
        embed.set_image(url=event['image'])
    
    return embed


async def get_or_create_thread(event: dict, event_id: str, title: str):
    """Получает существующую ветку или создаёт новую на посте мероприятия"""
    if event.get('thread_id'):
        try:
            thread = await client.fetch_channel(event['thread_id'])
            return thread
        except Exception:
            pass
    
    try:
        channel = await client.fetch_channel(event['channel_id'])
        message = await channel.fetch_message(event['message_id'])
        thread = await message.create_thread(name=f"💬 {title}")
        
        # Сохраняем ID ветки
        events = load_json(EVENTS_FILE, {})
        if event_id in events:
            events[event_id]['thread_id'] = thread.id
            save_json(EVENTS_FILE, events)
        
        return thread
    except Exception as e:
        print(f"❌ Ошибка создания ветки: {e}")
        return None


async def create_event(title: str, description: str, start_time: datetime, end_time: datetime, 
                      image_url: str = None, color: int = 15844367):
    event_id = str(uuid.uuid4())
    events = load_json(EVENTS_FILE, {})
    
    events[event_id] = {
        'title': title,
        'description': description,
        'start_time': int(start_time.timestamp()),
        'end_time': int(end_time.timestamp()),
        'accepted': {},
        'declined': {},
        'image': image_url,
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
        # View БЕЗ аргументов — будет искать event_id по message_id
        view = EventView()
        
        # Отправляем пост мероприятия
        message = await channel.send(embed=embed, view=view)
        events[event_id]['message_id'] = message.id
        
        # Создаём ветку на посту мероприятия
        thread = await message.create_thread(name=f"💬 {title}")
        events[event_id]['thread_id'] = thread.id
        
        # Ищем роль "Боец ArmA"
        role = discord.utils.get(guild.roles, name="Боец ArmA")
        if role:
            role_mention = role.mention
            await thread.send(
                f"{role_mention}\n\n" +
                es("📢 Бойцы, запланировано мероприятие! Ждем ваших отметок!")
            )
            print(f"✅ Упоминание роли 'Боец ArmA' отправлено в ветку '{title}'")
        else:
            print("⚠️ Роль 'Боец ArmA' не найдена на сервере")
        
        save_json(EVENTS_FILE, events)
        print(f"✅ Мероприятие '{title}' опубликовано")
        
    except Exception as e:
        print(f"❌ Ошибка публикации мероприятия: {e}")

async def show_event_list(interaction: discord.Interaction):
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
        text += es(f"✅ Придут: {len(event.get('accepted', {}))}\n")
        text += es(f"❌ Не придут: {len(event.get('declined', {}))}\n")
        rem_2d = es("✅ Отправлено") if event.get('reminder_2days_sent') else es("⏳ Ожидается")
        rem_15m = es("✅ Отправлено") if event.get('reminder_15min_sent') else es("⏳ Ожидается")
        text += f"Напоминание за 2 дня: {rem_2d}\n"
        text += f"Напоминание за 15 мин: {rem_15m}\n\n"
    
    await interaction.response.send_message(text, ephemeral=True)


async def post_weekly_events():
    print(es("📅 Публикация мероприятий на эту неделю..."))
    
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
        "СУББОТА - RTvT AS VDV",
        f"Бойцы, на субботу запланировано мероприятие на сервере AS VDV. Ждём вас! Заходите в голосовой канал {voice_mention} за 15 минут до начала.",
        saturday_start,
        saturday_end
    )
    
    await asyncio.sleep(2)
    
    await create_event(
        "ВОСКРЕСЕНЬЕ - TvT TT",
        f"Бойцы, на воскресенье запланировано мероприятие на сервере TT. Ждём вас! Заходите в голосовой канал {voice_mention} за 15 минут до начала.",
        sunday_start,
        sunday_end
    )


async def check_event_reminders():
    """Проверяет все мероприятия и отправляет два типа напоминаний:
    - за 2 суток (48 часов): пингует не определившихся
    - за 15 минут: пингует зарегистрировавшихся
    Оба напоминания отправляются в ветку поста мероприятия."""
    events = load_json(EVENTS_FILE, {})
    current_time = datetime.now(MSK)
    changed = False
    
    for event_id, event in events.items():
        try:
            event_start = datetime.fromtimestamp(event['start_time'], MSK)
            time_until_start = event_start - current_time
            
            # Напоминание за 2 суток (от 47 до 48 часов до начала)
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
                                if member:
                                    mentions.append(member.mention)
                                else:
                                    mentions.append(f"**{nickname}**")
                            
                            mentions_text = " ".join(mentions)
                            voice_mention = f"<#{VOICE_CHANNEL_ID}>"
                            
                            reminder_text = (
                                es(f"📋 **Внимание: {event['title']}**\n\n") +
                                f"{mentions_text}\n\n" +
                                es("⏳ До мероприятия осталось **2 суток**, а вы ещё не отметились!\n\n") +
                                es("👉 Пожалуйста, отметьтесь в основном посте мероприятия — нажмите ") +
                                es("✅ **Приду** или ") + es("❌ **Не приду**.\n\n") +
                                es("💬 Это помогает командованию планировать состав на игру. ") +
                                f"Сбор в голосовом канале {voice_mention}."
                            )
                            
                            await thread.send(reminder_text)
                            print(f"✅ Напоминание за 2 суток для '{event['title']}' ({len(unmarked)} неопределившихся)")
                    
                    event['reminder_2days_sent'] = True
                    changed = True
            
            # Напоминание за 15 минут
            if not event.get('reminder_15min_sent', False):
                if timedelta(0) <= time_until_start <= timedelta(minutes=15):
                    accepted = list(event.get('accepted', {}).keys())
                    
                    if not accepted:
                        print(f"⚠️ Напоминание для '{event['title']}': нет зарегистрировавшихся")
                        event['reminder_15min_sent'] = True
                        changed = True
                        continue
                    
                    thread = await get_or_create_thread(event, event_id, event['title'])
                    if thread:
                        mentions = []
                        for nickname in accepted:
                            member = await find_member_by_nickname(nickname)
                            if member:
                                mentions.append(member.mention)
                            else:
                                mentions.append(f"**{nickname}**")
                        
                        mentions_text = " ".join(mentions)
                        voice_mention = f"<#{VOICE_CHANNEL_ID}>"
                        
                        reminder_text = (
                            es(f"🔔 **Бойцы, внимание: {event['title']}** 🔔\n\n") +
                            f"{mentions_text}\n\n" +
                            es(f"⚡ Мероприятие начнется через **{int(time_until_start.total_seconds() // 60)} минут**!\n\n") +
                            es("📍 Ждем всех на сборах! Заходите в голосовой канал:\n") +
                            es(f"👉 **{voice_mention}**\n\n") +
                            "Не забудьте подготовиться к началу мероприятия!"
                        )
                        
                        await thread.send(reminder_text)
                        print(f"✅ Напоминание за 15 минут для '{event['title']}' ({len(accepted)} бойцов)")
                    
                    event['reminder_15min_sent'] = True
                    changed = True
        
        except Exception as e:
            print(f"Ошибка проверки напоминания для {event_id}: {e}")
    
    if changed:
        save_json(EVENTS_FILE, events)


# ============== СОБЫТИЯ DISCORD ==============

@client.event
async def on_ready():
    print(f'Бот запущен как {client.user} (PID {os.getpid()})')
    
    # Предзагружаем список участников клана
    await load_clan_members_from_sheet()

    if not scheduler.get_job('spreadsheet_check'):
        scheduler.add_job(
            check_spreadsheet,
            'cron',
            day='*/2',
            hour=18,
            minute=0,
            id='spreadsheet_check',
            replace_existing=True
        )

    if not scheduler.get_job('weekly_events'):
        scheduler.add_job(
            post_weekly_events,
            'cron',
            day_of_week='mon',
            hour=12,
            minute=0,
            id='weekly_events',
            replace_existing=True
        )
        print(es("📅 Планировщик еженедельных мероприятий добавлен"))

    if not scheduler.get_job('vacation_check'):
        scheduler.add_job(
            check_expired_vacations,
            'interval',
            hours=1,
            id='vacation_check',
            replace_existing=True
        )
        print(es("🏖️ Планировщик проверки отпусков добавлен (каждый час)"))

    if not scheduler.get_job('event_reminders'):
        scheduler.add_job(
            check_event_reminders,
            'interval',
            minutes=1,
            id='event_reminders',
            replace_existing=True
        )
        print(es("🔔 Планировщик напоминаний мероприятий добавлен (каждую минуту)"))

    # Обновление кэша списка участников клана каждый час
    if not scheduler.get_job('clan_cache_refresh'):
        scheduler.add_job(
            load_clan_members_from_sheet,
            'interval',
            hours=1,
            id='clan_cache_refresh',
            replace_existing=True
        )
        print(es("📋 Планировщик обновления кэша участников добавлен (каждый час)"))

    if not scheduler.running:
        scheduler.start()
        print("Планировщик запущен")
    
    # Регистрируем ВСЕ View БЕЗ аргументов — они будут искать данные по message_id
    client.add_view(AdminMainMenuView())
    client.add_view(VacationRequestView())
    client.add_view(VacationApprovalView())
    client.add_view(VacationMessageView())
    client.add_view(EventView())
    print("✅ UI компоненты зарегистрированы")
    
    try:
        admin_channel = await client.fetch_channel(ADMIN_CHANNEL_ID)
        
        # === УДАЛЯЕМ СТАРЫЕ СООБЩЕНИЯ БОТА В АДМИНСКОМ КАНАЛЕ ===
        try:
            deleted = await admin_channel.purge(
                limit=None,
                check=lambda m: m.author == client.user,
                reason="Очистка старых сообщений панели управления"
            )
            if deleted:
                print(f"🧹 Удалено {len(deleted)} старых сообщений бота в админском канале")
        except discord.Forbidden:
            print("⚠️ У бота нет прав на удаление сообщений в админском канале. Проверьте права роли бота (Manage Messages).")
        except discord.HTTPException as e:
            print(f"⚠️ Не удалось удалить старые сообщения: {e}")
        except Exception as e:
            print(f"⚠️ Неожиданная ошибка при очистке: {e}")
        
        # === ОТПРАВЛЯЕМ НОВОЕ СООБЩЕНИЕ С ПАНЕЛЬЮ ===
        embed = discord.Embed(
            title=es("🛠️ Панель управления комбата и заместителей"),
            description=(
                "Здесь вы можете управлять всеми функциями бота через кнопки. "
                "Функции бота разделены по строчкам, которые соответствуют кнопкам ниже:\n\n"
                "1. Мероприятия \n"
                "2. Сообщения \n"
                "3. Отпуска \n"
                "4. Утилиты \n"
            ),
            color=discord.Color.blue()
        )
        view = AdminMainMenuView()
        await admin_channel.send(embed=embed, view=view)
        print("✅ Админ-меню опубликовано")
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
            await message.channel.send(es("⚠️ Проверка уже выполняется, подождите."))
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