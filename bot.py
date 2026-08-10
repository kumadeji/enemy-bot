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

VACATION_EXCEPTIONS = {
    '[En-Y]Mr.GreyGoose': (datetime(2026, 8, 4), datetime(2026, 8, 10)),
    '[En-Y]Bercekle': (datetime(2026, 7, 26), datetime(2026, 8, 26)),
    '[En-Y]Slay': (datetime(2026, 7, 27), datetime(2026, 8, 27)),
    '[En-Y]Killa': (datetime(2026, 7, 23), datetime(2026, 8, 23)),
    '[En-Y]v1c': (datetime(2026, 7, 23), datetime(2026, 8, 23)),
    '[En-Y]Russo': (datetime(2026, 7, 30), datetime(2026, 8, 10)),
    '[En-Y]GDim': (datetime(2026, 8, 4), datetime(2026, 9, 4)),
}

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
ADMIN_CHANNEL_ID = 1470352063824265279
VOICE_CHANNEL_URL = "https://discord.com/channels/734494109032513699/1284893513921728582"

CLAN_MEMBERS = [
    "[En-Y]COKOJl", "[En-Y]Killa", "[En-Y]SIR", "[En-Y]Russo", "[En-Y]v1c",
    "[En-Y]Bercekle", "[En-Y]BURBON", "[En-Y]Sterben", "[En-Y]Bushwacker",
    "[En-Y]Prof. Kruglov", "[En-Y]Boba", "[En-Y]RainStars", "[En-Y]GDim",
    "[En-Y]Sliva88", "[En-Y]Mr.GreyGoose", "[En-Y]Libertad", "[En-Y]Komarant",
    "[En-Y]STARICHOK", "[En-Y]Sasce2044", "[En-Y]Metalist08", "[En-Y]Slay",
    "[En-Y]Vampire", "[En-Y]MAKER", "[En-Y]GhostCTPAX"
]

EVENTS_FILE = 'events_data.json'
VACATIONS_FILE = 'vacations.json'

VACATION_RULES = """📋 **ПРАВИЛА ПОДАЧИ РАПОРТА**

⚠️ **Важно:** Если ты отсутствуешь более 7 дней, оформи отпуск, чтобы не быть исключённым из клана за низкую активность.

**📌 Основные правила:**
• Отпуск оформляется на срок от **7 дней до 1 месяца**
• Рапорт можно продлить, создав новый со следующего дня после окончания предыдущего
• Во время отпуска тебе **не нужно отмечаться в расписании на игры**
• Игрок в отпуске **лишается возможности участия в играх** до закрытия отпуска

**✅ Уважительные причины:**
• Командировки и мероприятия по работе
• Семейные мероприятия
• Проблемы со здоровьем
• Длительные учебные мероприятия (например, сессия)

**❌ Неуважительные причины (отпуск отклоняется):**
• Усталость от игры

**💡 Совет:** Указывай честную и конкретную причину. Это помогает командованию планировать состав на игры. Отпуск может быть аннулирован, если вы будете находится в отпуске, но постоянно играть в игры во время проводимых мероприятий в клане.
"""

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


def is_on_vacation(nickname: str, current_date: datetime) -> bool:
    clean_nickname = nickname.strip().lower()
    current_date_only = current_date.date()

    for vac_name, (start_date, end_date) in VACATION_EXCEPTIONS.items():
        if clean_nickname == vac_name.strip().lower():
            if start_date.date() <= current_date_only <= end_date.date():
                print(f"✅ {nickname} в отпуске (до {end_date.strftime('%d.%m.%Y')}), пропускаем.")
                return True
            else:
                return False
    return False


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
        "🔔 **Проверка ошибок с регистрациями** 🔔",
        "",
        "Это автоматическая проверка клана по таблице — всех, кто не в отпуске. "
        "**[Полная таблица](<https://enemygaming.netlify.app/temptable>)** — обновляется ежедневно.",
        "Если вы исправили какую-либо проблему, поставьте лайк как реакцию на это сообщение.",
        "🔴 **Красные** проблемы — критические, требуют немедленного исправления.",
        "🟡 **Желтые** проблемы — менее важные, но тоже требуют своевременного исправления.",
        f"📅 Проверка от {current_time.strftime('%d.%m.%Y %H:%M')} МСК",
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
        parts.append("🔴 **Критические проблемы, требующие скорейшего исправления:**")
        for issue in red_issues:
            parts.append(issue_line(issue))
        parts.append("")

    if yellow_issues:
        parts.append(
            "🟡 **Важные, но менее критические проблемы, "
            "также требующие своевременного исправления:**"
        )
        for issue in yellow_issues:
            parts.append(issue_line(issue))
        parts.append("")

    parts.append("─" * 50)

    return "\n".join(parts).strip("\n")


async def check_spreadsheet():
    if check_lock.locked():
        print("⚠️ Проверка уже выполняется, пропускаем.")
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

                if is_on_vacation(nickname, current_time):
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
                        "⚠️ **Не удалось найти в Discord:**\n"
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
    if is_on_vacation(nickname, current_date):
        return True
    
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


def get_active_members(current_date: datetime) -> list:
    return [m for m in CLAN_MEMBERS if not is_on_vacation_dynamic(m, current_date)]


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
                await member.add_roles(role, reason="Оформление отпуска")
                print(f"✅ Роль 'Отпуск' добавлена {member.display_name}")
        else:
            if role in member.roles:
                await member.remove_roles(role, reason="Завершение отпуска")
                print(f"✅ Роль 'Отпуск' снята с {member.display_name}")
    except Exception as e:
        print(f"❌ Ошибка обновления роли: {e}")


# ============== UI КОМПОНЕНТЫ ==============

class VacationModal(discord.ui.Modal, title="🏖️ Оформление отпуска"):
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


class AdminVacationModal(discord.ui.Modal, title="🏖️ Отпуск для игрока (комбат)"):
    player_name = discord.ui.TextInput(
        label="Ник игрока (как в Discord)",
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


class SendMessageModal(discord.ui.Modal, title="📝 Отправка сообщения"):
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
            await interaction.response.send_message("✅ Сообщение отправлено!", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("❌ Неверный ID канала!", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)


class DeleteMessageModal(discord.ui.Modal, title="🗑️ Удаление сообщения"):
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
            await interaction.response.send_message("✅ Сообщение удалено!", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("❌ Неверный ID!", ephemeral=True)
        except discord.NotFound:
            await interaction.response.send_message("❌ Сообщение не найдено!", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)


class EventCreateModal(discord.ui.Modal, title="📅 Создание мероприятия"):
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
            await interaction.response.send_message("✅ Мероприятие создано!", ephemeral=True)
        except ValueError:
            await interaction.response.send_message("❌ Неверный формат даты! Используйте ДД.ММ.ГГГГ ЧЧ:ММ", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)


class AdminMainMenuView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    # ═══════════ РЯД 1: МЕРОПРИЯТИЯ ═══════════
    @discord.ui.button(label="📅 Создать мероприятие", style=discord.ButtonStyle.primary, custom_id="admin_create_event", row=0)
    async def create_event_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message("⛔ Доступно только комбату и его заместителям!", ephemeral=True)
            return
        modal = EventCreateModal()
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="📅 Мероприятия недели", style=discord.ButtonStyle.primary, custom_id="admin_force_weekly", row=0)
    async def force_weekly_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message("⛔ Доступно только комбату и его заместителям!", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await post_weekly_events()
        await interaction.followup.send("✅ Мероприятия на эту неделю опубликованы!", ephemeral=True)
    
    @discord.ui.button(label="📋 Список мероприятий", style=discord.ButtonStyle.secondary, custom_id="admin_event_list", row=0)
    async def event_list_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message("⛔ Доступно только комбату и его заместителям!", ephemeral=True)
            return
        await show_event_list(interaction)
    
    # ═══════════ РЯД 2: СООБЩЕНИЯ ═══════════
    @discord.ui.button(label="📝 Отправить сообщение", style=discord.ButtonStyle.success, custom_id="admin_send_message", row=1)
    async def send_message_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message("⛔ Доступно только комбату и его заместителям!", ephemeral=True)
            return
        modal = SendMessageModal()
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="🗑️ Удалить сообщение", style=discord.ButtonStyle.danger, custom_id="admin_delete_message", row=1)
    async def delete_message_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message("⛔ Доступно только комбату и его заместителям!", ephemeral=True)
            return
        modal = DeleteMessageModal()
        await interaction.response.send_modal(modal)
    
    # ═══════════ РЯД 3: ОТПУСКА ═══════════
    @discord.ui.button(label="🏖️ Отпуск для игрока", style=discord.ButtonStyle.primary, custom_id="admin_vacation_for_player", row=2)
    async def vacation_for_player_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message("⛔ Доступно только комбату и его заместителям!", ephemeral=True)
            return
        modal = AdminVacationModal()
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label="📋 Правила отпусков", style=discord.ButtonStyle.secondary, custom_id="admin_vacation_rules", row=2)
    async def vacation_rules_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message("⛔ Доступно только комбату и его заместителям!", ephemeral=True)
            return
        await publish_vacation_info(interaction)
    
    @discord.ui.button(label="🏖️ Список отпусков", style=discord.ButtonStyle.secondary, custom_id="admin_vacation_list", row=2)
    async def vacation_list_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message("⛔ Доступно только комбату и его заместителям!", ephemeral=True)
            return
        await show_vacation_list(interaction)
    
    # ═══════════ РЯД 4: УТИЛИТЫ ═══════════
    @discord.ui.button(label="🔍 Проверить таблицу", style=discord.ButtonStyle.success, custom_id="admin_check_table", row=3)
    async def check_table_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message("⛔ Доступно только комбату и его заместителям!", ephemeral=True)
            return
        if check_lock.locked():
            await interaction.response.send_message("⚠️ Проверка уже выполняется!", ephemeral=True)
            return
        await interaction.response.send_message("🔍 Запускаю проверку таблицы...", ephemeral=True)
        await check_spreadsheet()
    
    @discord.ui.button(label="❓ Справка", style=discord.ButtonStyle.secondary, custom_id="admin_faq", row=3)
    async def faq_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message("⛔ Доступно только комбату и его заместителям!", ephemeral=True)
            return
        await send_faq(interaction)


class VacationRequestView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(label="🏖️ Оформить отпуск", style=discord.ButtonStyle.primary, custom_id="vacation_request")
    async def vacation_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = VacationModal()
        await interaction.response.send_modal(modal)


class VacationMessageView(discord.ui.View):
    def __init__(self, nickname: str):
        super().__init__(timeout=None)
        self.nickname = nickname
    
    @discord.ui.button(label="✅ Завершить отпуск досрочно", style=discord.ButtonStyle.success, custom_id="vacation_end_early")
    async def end_early_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.display_name != self.nickname and interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message("⛔ Только сам игрок или командование может закрыть отпуск!", ephemeral=True)
            return
        await close_vacation(interaction, self.nickname, early=True, by_admin=False)
    
    @discord.ui.button(label="🔴 Закрыть отпуск (комбат)", style=discord.ButtonStyle.danger, custom_id="vacation_admin_close")
    async def admin_close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message("⛔ Доступно только комбату и его заместителям!", ephemeral=True)
            return
        await close_vacation(interaction, self.nickname, early=True, by_admin=True)


class EventView(discord.ui.View):
    def __init__(self, event_id: str):
        super().__init__(timeout=None)
        self.event_id = event_id
    
    @discord.ui.button(label="✅ Приду", style=discord.ButtonStyle.success, custom_id="event_accept")
    async def accept_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_event_response(interaction, self.event_id, "accept")
    
    @discord.ui.button(label="❌ Не приду", style=discord.ButtonStyle.danger, custom_id="event_decline")
    async def decline_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_event_response(interaction, self.event_id, "decline")


class AdminEventView(discord.ui.View):
    def __init__(self, event_id: str):
        super().__init__(timeout=None)
        self.event_id = event_id
    
    @discord.ui.button(label="🔄 Обновить список", style=discord.ButtonStyle.primary, custom_id="event_refresh")
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message("⛔ Доступно только комбату и его заместителям!", ephemeral=True)
            return
        await refresh_event_message(interaction, self.event_id)
    
    @discord.ui.button(label="❌ Отменить мероприятие", style=discord.ButtonStyle.danger, custom_id="event_cancel")
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in ADMIN_USER_IDS:
            await interaction.response.send_message("⛔ Доступно только комбату и его заместителям!", ephemeral=True)
            return
        await cancel_event(interaction, self.event_id)


# ============== ФУНКЦИИ ОТПУСКОВ ==============

async def publish_vacation_info(interaction: discord.Interaction):
    try:
        channel = await client.fetch_channel(VACATION_CHANNEL_ID)
        
        embed = discord.Embed(
            title="🏖️ СИСТЕМА ОТПУСКОВ КЛАНА",
            description=VACATION_RULES,
            color=discord.Color.green()
        )
        embed.set_footer(text="Нажмите кнопку ниже, чтобы оформить отпуск")
        
        view = VacationRequestView()
        await channel.send(embed=embed, view=view)
        
        await interaction.response.send_message("✅ Правила отпусков опубликованы!", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)


async def handle_vacation_request(interaction: discord.Interaction, nickname: str, start_str: str, end_str: str, reason: str, by_admin: bool):
    try:
        start_date = datetime.strptime(start_str, "%d.%m.%Y")
        end_date = datetime.strptime(end_str, "%d.%m.%Y")
        
        duration = (end_date - start_date).days
        if duration < 7:
            await interaction.response.send_message("❌ Отпуск должен быть не менее 7 дней!", ephemeral=True)
            return
        if duration > 31:
            await interaction.response.send_message("❌ Отпуск не может быть дольше 31 дня!", ephemeral=True)
            return
        
        if start_date.date() < datetime.now(MSK).date():
            await interaction.response.send_message("❌ Дата начала должна быть в будущем!", ephemeral=True)
            return
        
        member = await find_member_by_nickname(nickname)
        if not member:
            await interaction.response.send_message(f"❌ Игрок {nickname} не найден на сервере!", ephemeral=True)
            return
        
        vacations = load_json(VACATIONS_FILE, {})
        
        if nickname in vacations and vacations[nickname].get('status') == 'active':
            await interaction.response.send_message(f"⚠️ У {nickname} уже есть активный отпуск!", ephemeral=True)
            return
        
        vacations[nickname] = {
            'start': start_date.isoformat(),
            'end': end_date.isoformat(),
            'reason': reason,
            'requested_at': datetime.now(MSK).isoformat(),
            'status': 'active',
            'message_id': None,
            'channel_id': None,
            'created_by': interaction.user.display_name if by_admin else 'Сам игрок'
        }
        save_json(VACATIONS_FILE, vacations)
        
        await update_vacation_role(member, True)
        
        channel = await client.fetch_channel(VACATION_CHANNEL_ID)
        
        embed = discord.Embed(
            title="🏖️ Отпуск оформлен",
            description=f"**{nickname}** взял(а) отпуск",
            color=discord.Color.green()
        )
        embed.add_field(name="📅 Период", value=f"{start_str} - {end_str} ({duration} дней)", inline=True)
        embed.add_field(name="📝 Причина", value=reason, inline=False)
        if by_admin:
            embed.add_field(name="👤 Оформил", value=f"Комбат/зам: {interaction.user.display_name}", inline=False)
        embed.add_field(name="ℹ️ Статус", value="✅ Активен", inline=False)
        embed.set_footer(text="Во время отпуска вам не нужно отмечаться в расписании мероприятий")
        
        view = VacationMessageView(nickname)
        message = await channel.send(embed=embed, view=view)
        
        vacations[nickname]['message_id'] = message.id
        vacations[nickname]['channel_id'] = channel.id
        save_json(VACATIONS_FILE, vacations)
        
        if by_admin:
            await interaction.response.send_message(
                f"✅ Отпуск для {nickname} оформлен с {start_str} по {end_str}!\n"
                f"Во время отпуска ему не нужно будет отмечаться в расписании мероприятий.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"✅ Отпуск оформлен с {start_str} по {end_str}!\n"
                f"Во время отпуска вам не нужно будет отмечаться в расписании мероприятий.",
                ephemeral=True
            )
        
        print(f"🏖️ {nickname} оформил отпуск с {start_str} по {end_str}. Причина: {reason}. Админ: {by_admin}")
        
    except ValueError:
        await interaction.response.send_message(
            "❌ Неверный формат даты! Используйте ДД.ММ.ГГГГ (например, 15.08.2026)",
            ephemeral=True
        )
    except Exception as e:
        print(f"Ошибка оформления отпуска: {e}")
        await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)


async def close_vacation(interaction: discord.Interaction, nickname: str, early: bool = False, by_admin: bool = False):
    vacations = load_json(VACATIONS_FILE, {})
    
    if nickname not in vacations:
        await interaction.response.send_message("❌ Отпуск не найден!", ephemeral=True)
        return
    
    vacation = vacations[nickname]
    if vacation.get('status') != 'active':
        await interaction.response.send_message("⚠️ Отпуск уже закрыт!", ephemeral=True)
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
            status_text = "❌ Завершен досрочно" if early else "✅ Завершен по истечению срока"
            for i, field in enumerate(embed.fields):
                if field.name == "ℹ️ Статус":
                    embed.set_field_at(i, name="ℹ️ Статус", value=status_text, inline=False)
                    break
            embed.color = discord.Color.red() if early else discord.Color.greyple()
            await message.edit(embed=embed, view=None)
    except Exception as e:
        print(f"Ошибка обновления сообщения: {e}")
    
    who_closed = "комбат/заместитель" if by_admin else "игрок"
    await interaction.response.send_message(
        f"✅ Отпуск {nickname} закрыт ({who_closed}). Роль 'Отпуск' снята.",
        ephemeral=True
    )
    print(f"✅ Отпуск {nickname} закрыт. Ранний: {early}, Админ: {by_admin}")


async def show_vacation_list(interaction: discord.Interaction):
    vacations = load_json(VACATIONS_FILE, {})
    active_vacations = {k: v for k, v in vacations.items() if v.get('status') == 'active'}
    
    if not active_vacations:
        await interaction.response.send_message("🏖️ Нет активных отпусков", ephemeral=True)
        return
    
    text = "🏖️ **Активные отпуска:**\n\n"
    for nickname, data in active_vacations.items():
        start = datetime.fromisoformat(data['start']).strftime('%d.%m.%Y')
        end = datetime.fromisoformat(data['end']).strftime('%d.%m.%Y')
        text += f"**{nickname}**: {start} - {end}\n"
        text += f"Причина: {data.get('reason', 'Не указана')}\n"
        text += f"Оформил: {data.get('created_by', 'Сам игрок')}\n\n"
    
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
                                if field.name == "ℹ️ Статус":
                                    embed.set_field_at(i, name="ℹ️ Статус", value="✅ Завершен по истечению срока", inline=False)
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
        await interaction.response.send_message("❌ Мероприятие не найдено!", ephemeral=True)
        return
    
    event = events[event_id]
    nickname = interaction.user.display_name
    current_date = datetime.now(MSK)
    
    if is_on_vacation_dynamic(nickname, current_date):
        await interaction.response.send_message(
            "🏖️ Вы сейчас в отпуске. Во время отпуска вам не нужно отмечаться в расписании мероприятий.",
            ephemeral=True
        )
        return
    
    if response_type == "accept":
        event['accepted'][nickname] = True
        event['declined'].pop(nickname, None)
        await interaction.response.send_message("✅ Вы записаны на мероприятие!", ephemeral=True)
    else:
        event['declined'][nickname] = True
        event['accepted'].pop(nickname, None)
        await interaction.response.send_message("❌ Вы отказались от участия!", ephemeral=True)
    
    save_json(EVENTS_FILE, events)
    
    try:
        channel = await client.fetch_channel(event['channel_id'])
        message = await channel.fetch_message(event['message_id'])
        embed = build_event_embed(event_id)
        await message.edit(embed=embed)
    except Exception as e:
        print(f"Ошибка обновления сообщения: {e}")


async def refresh_event_message(interaction: discord.Interaction, event_id: str):
    events = load_json(EVENTS_FILE, {})
    if event_id not in events:
        await interaction.response.send_message("❌ Мероприятие не найдено!", ephemeral=True)
        return
    
    event = events[event_id]
    
    try:
        channel = await client.fetch_channel(event['channel_id'])
        message = await channel.fetch_message(event['message_id'])
        embed = build_event_embed(event_id)
        await message.edit(embed=embed)
        await interaction.response.send_message("✅ Список участников обновлен!", ephemeral=True)
    except Exception as e:
        print(f"Ошибка обновления: {e}")
        await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)


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
        await interaction.response.send_message("✅ Мероприятие отменено!", ephemeral=True)
    else:
        await interaction.response.send_message("❌ Мероприятие не найдено!", ephemeral=True)


def build_event_embed(event_id: str) -> discord.Embed:
    events = load_json(EVENTS_FILE, {})
    event = events[event_id]
    
    current_date = datetime.now(MSK)
    active_members = get_active_members(current_date)
    
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
        name="⏰ Время",
        value=f"<t:{start_ts}:F> - <t:{end_ts}:t>\n<:emoji:878391707727716413> <t:{start_ts}:R>",
        inline=False
    )
    
    if accepted:
        embed.add_field(
            name=f"<:accepted:713124484436983971> Придут ({len(accepted)})",
            value=">>> " + "\n".join(accepted),
            inline=True
        )
    if declined:
        embed.add_field(
            name=f"<:declined:713124484688642068> Не придут ({len(declined)})",
            value=">>> " + "\n".join(declined),
            inline=True
        )
    if unmarked:
        embed.add_field(
            name=f"❓ Не определились ({len(unmarked)})",
            value=">>> " + "\n".join(unmarked),
            inline=False
        )
    
    if event.get('image'):
        embed.set_image(url=event['image'])
    
    return embed


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
        'reminder_sent': False  # Флаг для напоминания за 15 минут
    }
    save_json(EVENTS_FILE, events)
    
    try:
        channel = await client.fetch_channel(EVENTS_CHANNEL_ID)
        embed = build_event_embed(event_id)
        view = EventView(event_id)
        
        message = await channel.send(embed=embed, view=view)
        events[event_id]['message_id'] = message.id
        save_json(EVENTS_FILE, events)
        
        admin_embed = discord.Embed(
            title="🛠️ Админ-панель мероприятия",
            description=f"Мероприятие: **{title}**\nID: `{event_id}`",
            color=discord.Color.blue()
        )
        admin_view = AdminEventView(event_id)
        await channel.send(embed=admin_embed, view=admin_view)
        
        print(f"✅ Мероприятие '{title}' опубликовано")
    except Exception as e:
        print(f"❌ Ошибка публикации мероприятия: {e}")


async def show_event_list(interaction: discord.Interaction):
    events = load_json(EVENTS_FILE, {})
    if not events:
        await interaction.response.send_message("📭 Нет активных мероприятий", ephemeral=True)
        return
    
    text = "📋 **Активные мероприятия:**\n\n"
    for event_id, event in events.items():
        start = datetime.fromtimestamp(event['start_time'], MSK)
        text += f"**{event['title']}**\n"
        text += f"ID: `{event_id}`\n"
        text += f"Дата: {start.strftime('%d.%m.%Y %H:%M')}\n"
        text += f"✅ Придут: {len(event.get('accepted', {}))}\n"
        text += f"❌ Не придут: {len(event.get('declined', {}))}\n"
        text += f"🔔 Напоминание: {'✅ Отправлено' if event.get('reminder_sent') else '⏳ Ожидается'}\n\n"
    
    await interaction.response.send_message(text, ephemeral=True)


async def post_weekly_events():
    """Публикует мероприятия на эту неделю (суббота и воскресенье)"""
    print("📅 Публикация мероприятий на эту неделю...")
    
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
    
    await create_event(
        "СУББОТА - RTvT AS VDV",
        "Бойцы, на субботу запланировано мероприятие на сервере AS VDV. Требуется прибыть на мероприятие и в голосовой канал \"Мероприятие\" за 15 минут до начала игры.",
        saturday_start,
        saturday_end
    )
    
    await asyncio.sleep(2)
    
    await create_event(
        "ВОСКРЕСЕНЬЕ - TvT TT",
        "Бойцы, на воскресенье запланировано мероприятие на сервере TT. Требуется прибыть на мероприятие и в голосовой канал \"Мероприятие\" за 15 минут до начала игры.",
        sunday_start,
        sunday_end
    )


# ============== НОВАЯ ФУНКЦИЯ: НАПОМИНАНИЯ ЗА 15 МИНУТ ==============

async def check_event_reminders():
    """Проверяет все мероприятия и отправляет напоминания за 15 минут до начала"""
    events = load_json(EVENTS_FILE, {})
    current_time = datetime.now(MSK)
    changed = False
    
    for event_id, event in events.items():
        # Пропускаем уже отправленные напоминания
        if event.get('reminder_sent', False):
            continue
        
        try:
            event_start = datetime.fromtimestamp(event['start_time'], MSK)
            time_until_start = event_start - current_time
            
            # Если до начала от 0 до 15 минут (и уже не началось)
            if timedelta(0) <= time_until_start <= timedelta(minutes=15):
                accepted = list(event.get('accepted', {}).keys())
                
                if not accepted:
                    print(f"⚠️ Напоминание для '{event['title']}': нет зарегистрировавшихся")
                    event['reminder_sent'] = True
                    changed = True
                    continue
                
                # Отправляем напоминание
                try:
                    channel = await client.fetch_channel(event['channel_id'])
                    
                    # Ищем member объекты для упоминаний
                    mentions = []
                    for nickname in accepted:
                        member = await find_member_by_nickname(nickname)
                        if member:
                            mentions.append(member.mention)
                        else:
                            mentions.append(f"**{nickname}**")
                    
                    mentions_text = " ".join(mentions)
                    
                    reminder_text = (
                        f"🔔 **Бойцы, внимание: {event['title']}** 🔔\n\n"
                        f"{mentions_text}\n\n"
                        f"⚡ Мероприятие начнется через **{int(time_until_start.total_seconds() // 60)} минут**!\n\n"
                        f"📍 Ждем всех на сборах! Заходите в голосовой канал:\n"
                        f"👉 **[#мероприятие](<{VOICE_CHANNEL_URL}>)**\n\n"
                        f"Не забудьте подготовиться к началу мероприятия!"
                    )
                    
                    await channel.send(reminder_text)
                    
                    event['reminder_sent'] = True
                    changed = True
                    
                    print(f"✅ Напоминание отправлено для '{event['title']}' ({len(accepted)} игроков)")
                    
                except Exception as e:
                    print(f"❌ Ошибка отправки напоминания для '{event['title']}': {e}")
        
        except Exception as e:
            print(f"Ошибка проверки напоминания для {event_id}: {e}")
    
    if changed:
        save_json(EVENTS_FILE, events)


# ============== КОМАНДЫ ==============

async def send_faq(interaction_or_message):
    faq_text = """📖 **СПРАВКА БОТА**

**🎯 Основные функции:**

**🔍 Проверка таблицы**
• Автоматически каждые 2 дня в 18:00 МСК
• Кнопка "🔍 Проверить таблицу" в админ-меню

**📅 Мероприятия**
• Автопубликация по понедельникам в 12:00 МСК
• Суббота 16:30-19:30 (RTvT AS VDV)
• Воскресенье 17:45-22:15 (TvT TT)
• Кнопка "📅 Мероприятия недели" для ручной публикации
• Игроки отмечаются кнопками ✅/❌
• Автоматические напоминания за 15 минут до начала

**🏖️ Отпуска**
• Игроки оформляют сами через кнопку в канале отпусков
• Комбат может оформить отпуск любому игроку
• Срок: 7-31 день
• Нужна уважительная причина
• Во время отпуска не нужно отмечаться на игры
• Автоматическая роль "Отпуск"
• Досрочное закрытие кнопкой

**🛠️ Для комбата и заместителей:**
Все функции через кнопки в админ-канале:
• Создание/отмена мероприятий
• Отправка/удаление сообщений в любой канал
• Оформление отпусков для игроков
• Публикация правил отпусков
• Проверка таблицы
"""
    if hasattr(interaction_or_message, 'response'):
        await interaction_or_message.response.send_message(faq_text, ephemeral=True)
    else:
        await send_chunked(interaction_or_message.channel, faq_text, "FAQ")
        await interaction_or_message.add_reaction('✅')


# ============== СОБЫТИЯ DISCORD ==============

@client.event
async def on_ready():
    print(f'Бот запущен как {client.user} (PID {os.getpid()})')

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
        print("📅 Планировщик еженедельных мероприятий добавлен")

    if not scheduler.get_job('vacation_check'):
        scheduler.add_job(
            check_expired_vacations,
            'interval',
            hours=1,
            id='vacation_check',
            replace_existing=True
        )
        print("🏖️ Планировщик проверки отпусков добавлен (каждый час)")

    if not scheduler.get_job('event_reminders'):
        scheduler.add_job(
            check_event_reminders,
            'interval',
            minutes=1,
            id='event_reminders',
            replace_existing=True
        )
        print("🔔 Планировщик напоминаний мероприятий добавлен (каждую минуту)")

    if not scheduler.running:
        scheduler.start()
        print("Планировщик запущен")
    
    client.add_view(AdminMainMenuView())
    client.add_view(VacationRequestView())
    client.add_view(EventView("dummy"))
    client.add_view(AdminEventView("dummy"))
    print("✅ UI компоненты зарегистрированы")
    
    try:
        admin_channel = await client.fetch_channel(ADMIN_CHANNEL_ID)
        embed = discord.Embed(
            title="🛠️ Панель управления комбата и заместителей",
            description=(
                "Здесь вы можете управлять всеми функциями бота через кнопки.\nФункции бота разделены по строчкам, которые соответствуют кнопкам ниже:\n"
                "**1. Мероприятия**"
                "**2. Сообщения**"
                "**3. Отпуска**"
                "**4. Утилиты**"
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
            await message.channel.send('⚠️ Проверка уже выполняется, подождите.')
            return
        await message.channel.send('🔍 Запускаю проверку таблицы...')
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
        await message.channel.send(f'❌ Ошибка: {e}')


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