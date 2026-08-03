import discord
import os
import gspread
from datetime import datetime
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import asyncio

# ============== НАСТРОЙКИ ==============

# Discord
THREAD_ID = 1530860224724996237
ALLOWED_USER_ID = 115475534544109573
PREFIX = '!s '

# Google Sheets
GOOGLE_CREDENTIALS_FILE = 'credentials.json'
SPREADSHEET_URL = 'https://docs.google.com/spreadsheets/d/1QGc-SRkWnFCaSx56_46UJPRK0XOe33KPou7yJznbQBM'

# Timezone
MSK = pytz.timezone('Europe/Moscow')

# Vacation exceptions (отпуска)
VACATION_EXCEPTIONS = {
    '@[En-Y]Mr.GreyGoose': (datetime(2026, 8, 4), datetime(2026, 8, 10)),
    '@[En-Y]Bercekle': (datetime(2026, 7, 26), datetime(2026, 8, 26)),
    '@[En-Y]Slay': (datetime(2026, 7, 27), datetime(2026, 8, 27)),
    '@[En-Y]Killa': (datetime(2026, 7, 23), datetime(2026, 8, 23)),
    '@[En-Y]v1c': (datetime(2026, 7, 23), datetime(2026, 8, 23)),
    '@[En-Y]Russo': (datetime(2026, 7, 30), datetime(2026, 8, 10)),
}

# Column names
NICKNAME_COLUMN = 'Discord клана (с клантегом)'
SHEET_NAME = 'Основная таблица'

# Columns to check and their error values (ТОЛЬКО КРИТИЧЕСКИЕ / КРАСНЫЕ)
CHECKS = {
    'discord_echo': {
        'column': 'Discord ECHO (с клантегом)',
        'bad_values': ['Не указан', 'Не вступил'],
        'action': 'указать правильный позывной с клантегом в Discord-сервере ECHO',
        'severity': 'red'
    },
    'discord_as_vdv': {
        'column': 'Discord AS VDV (с клантегом)',
        'bad_values': ['Не указан', 'Не вступил'],
        'action': 'указать правильный позывной с клантегом в Discord-сервере AS VDV',
        'severity': 'red'
    },
    'discord_tt': {
        'column': 'Discord TT (с клантегом)',
        'bad_values': ['Не указан', 'Не вступил'],
        'action': 'указать правильный позывной с клантегом в Discord-сервере TT',
        'severity': 'red'
    },
    'steam_bourbon_friend': {
        'column': 'Steam (в друзьях у Бурбона?)',
        'bad_values': ['Нет'],
        'action': 'добавить Бурбона в друзья в Steam',
        'severity': 'red'
    },
    'site_clan': {
        'column': 'Сайт клана (без клантега)',
        'bad_values': ['Не зарегистрирован'],
        'action': 'зарегистрироваться на сайте клана',
        'severity': 'red'
    },
    'site_echo': {
        'column': 'Сайт ECHO (без клантега)',
        'bad_values': ['Не зарегистрирован'],
        'action': 'зарегистрироваться на сайте ECHO',
        'severity': 'red'
    },
    'site_as_vdv': {
        'column': 'Сайт AS VDV (без клантега)',
        'bad_values': ['Не зарегистрирован'],
        'action': 'зарегистрироваться на сайте AS VDV',
        'severity': 'red'
    },
    'site_tt': {
        'column': 'Сайт TT (без клантега)',
        'bad_values': ['Не зарегистрирован'],
        'action': 'зарегистрироваться на сайте TT',
        'severity': 'red'
    },
}

# ============== ИНИЦИАЛИЗАЦИЯ ==============

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

client = discord.Client(intents=intents)
scheduler = AsyncIOScheduler(timezone=MSK)

# Google Sheets
try:
    gc = gspread.service_account(filename=GOOGLE_CREDENTIALS_FILE)
except Exception as e:
    print(f"Ошибка при инициализации Google Sheets: {e}")
    gc = None

# ============== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==============

def is_on_vacation(nickname: str, current_date: datetime) -> bool:
    """Проверяет, находится ли пользователь в отпуске"""
    if nickname in VACATION_EXCEPTIONS:
        start_date, end_date = VACATION_EXCEPTIONS[nickname]
        current_date_only = current_date.date()
        if start_date.date() <= current_date_only <= end_date.date():
            return True
    return False

async def find_discord_user(nickname: str, thread):
    """Ищет пользователя Discord по нику (display_name)"""
    try:
        guild = thread.guild
        
        # Точное совпадение
        for member in guild.members:
            if member.display_name == nickname:
                return member
        
        # Нечеткий поиск (если в таблице написано чуть иначе, чем ник на сервере)
        for member in guild.members:
            if nickname.lower() in member.display_name.lower():
                return member
                
        return None
    except Exception as e:
        print(f"Ошибка при поиске пользователя {nickname}: {e}")
        return None

async def check_spreadsheet():
    """Основная функция проверки таблицы"""
    if not gc:
        print("Google Sheets не инициализирован")
        return
    
    print("Начинаем проверку таблицы (строки 1-28)...")
    
    try:
        spreadsheet = gc.open_by_url(SPREADSHEET_URL)
        sheet = spreadsheet.worksheet(SHEET_NAME)
        
        # Получаем данные строго с A1 по J28
        data = sheet.get('A1:J28')
        
        if not data or len(data) < 2:
            print("Таблица пуста или содержит только заголовки")
            return
        
        # Первая строка (индекс 0) - это заголовки столбцов
        headers = data[0]
        # Остальные строки (индексы 1 и далее, то есть строки 2-28) - это данные
        rows = data[1:]
        
        # Преобразуем список списков в список словарей
        records = []
        for row in rows:
            padded_row = row + [''] * (len(headers) - len(row))
            record_dict = {headers[i]: padded_row[i] for i in range(len(headers))}
            records.append(record_dict)
        
        if not records:
            print("Нет данных для проверки")
            return
        
        current_time = datetime.now(MSK)
        thread = await client.fetch_channel(THREAD_ID)
        
        user_issues = {}
        users_not_found = []
        
        for record in records:
            nickname = record.get(NICKNAME_COLUMN, '').strip()
            
            if not nickname:
                continue
            
            if is_on_vacation(nickname, current_time):
                print(f"Пользователь {nickname} в отпуске, пропускаем")
                continue
            
            issues = []
            for check_key, check_info in CHECKS.items():
                column_name = check_info['column']
                value = record.get(column_name, '').strip()
                
                if value in check_info['bad_values']:
                    issues.append({
                        'column': column_name,
                        'value': value,
                        'action': check_info['action'],
                        'severity': check_info['severity']
                    })
            
            if issues:
                discord_user = await find_discord_user(nickname, thread)
                
                if discord_user:
                    user_issues[discord_user] = issues
                else:
                    users_not_found.append(nickname)
                    print(f"Не удалось найти пользователя Discord для ника: {nickname}")
        
        if user_issues or users_not_found:
            await send_notification(thread, user_issues, users_not_found, current_time)
        else:
            print("Проблем в диапазоне A1:J28 не обнаружено")
            
    except Exception as e:
        print(f"Ошибка при проверке таблицы: {e}")
        try:
            thread = await client.fetch_channel(THREAD_ID)
            await thread.send(f"❌ Ошибка при проверке таблицы: {e}")
        except:
            pass

async def send_notification(thread, user_issues, users_not_found, current_time):
    """Формирует и отправляет сообщение с уведомлениями"""
    
    # Оставили только 'red', так как 'yellow' больше не используется
    issues_by_type = {
        'red': []
    }
    
    for user, issues in user_issues.items():
        for issue in issues:
            issues_by_type[issue['severity']].append({
                'user': user,
                'issue': issue
            })
    
    message_parts = []
    
    message_parts.append("🔔 **Проверяющий бот клана** 🔔\n\n")
    message_parts.append("Это автоматическая проверка клана — всех, кто не в отпуске. [Полная таблица с проблемами](https://enemygaming.netlify.app/temptable) — обновляется ежедневно.\n")
    message_parts.append("Если вы исправили какую-либо проблему, поставьте лайк как реакцию на это сообщение\n")
    message_parts.append("🔴 **Красные** проблемы — критические, требуют немедленного исправления. Бот проверяет только их\n")
    message_parts.append("🟡 **Желтые** проблемы — менее важные, но тоже требуют своевременного исправления. Бот их не проверяет - опирайтесь на таблицу выше\n")
    message_parts.append(f"📅 Проверка от {current_time.strftime('%d.%m.%Y %H:%M')} МСК\n")
    message_parts.append("─" * 50 + "\n\n")
    
    if issues_by_type['red']:
        message_parts.append("🔴 **КРИТИЧЕСКИЕ ПРОБЛЕМЫ** 🔴\n\n")
        
        red_issues_grouped = {}
        for item in issues_by_type['red']:
            action = item['issue']['action']
            if action not in red_issues_grouped:
                red_issues_grouped[action] = []
            red_issues_grouped[action].append(item['user'])
        
        for action, users in red_issues_grouped.items():
            message_parts.append(f"**Нужно: {action}**\n")
            user_mentions = [user.mention for user in users]
            message_parts.append(", ".join(user_mentions))
            message_parts.append("\n\n")
    
    if users_not_found:
        message_parts.append("─" * 50 + "\n\n")
        message_parts.append("⚠️ **Не удалось найти в Discord следующих пользователей:**\n")
        message_parts.append(", ".join(users_not_found))
        message_parts.append("\n(Проверьте правильность ников в таблице)\n")
    
    full_message = "".join(message_parts)
    
    if len(full_message) > 2000:
        await send_long_message(thread, full_message)
    else:
        await thread.send(full_message)

async def send_long_message(thread, message):
    """Отправляет длинное сообщение, разбивая его на части"""
    lines = message.split('\n')
    current_chunk = []
    current_length = 0
    
    for line in lines:
        line_length = len(line) + 1
        
        if current_length + line_length > 1900:
            await thread.send('\n'.join(current_chunk))
            current_chunk = []
            current_length = 0
            await asyncio.sleep(0.5)
        
        current_chunk.append(line)
        current_length += line_length
    
    if current_chunk:
        await thread.send('\n'.join(current_chunk))

# ============== СОБЫТИЯ DISCORD ==============

@client.event
async def on_ready():
    print(f'Бот запущен как {client.user}')
    
    print("Запускаем начальную проверку таблицы...")
    await check_spreadsheet()
    
    # Планировщик: каждые 2 дня в 18:00 по МСК
    scheduler.add_job(
        check_spreadsheet,
        'cron',
        day='*/2',
        hour=18,
        minute=0,
        id='spreadsheet_check'
    )
    
    if not scheduler.running:
        scheduler.start()
        print("Планировщик запущен. Следующая проверка через 2 дня в 18:00 МСК")

@client.event
async def on_message(message):
    if message.author == client.user:
        return

    if message.author.id != ALLOWED_USER_ID:
        return

    # Команда для ручной проверки таблицы
    if message.content.startswith('!check'):
        await message.channel.send('🔍 Запускаю проверку таблицы (строки 1-28)...')
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

    except discord.Forbidden:
        await message.channel.send('❌ У бота нет доступа к этому треду.')
    except discord.NotFound:
        await message.channel.send('❌ Тред не найден. Проверь THREAD_ID.')
    except Exception as e:
        await message.channel.send(f'❌ Ошибка: {e}')

# Запуск бота
if __name__ == '__main__':
    client.run(os.environ['DISCORD_TOKEN'])