import discord
import os

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)

THREAD_ID = 1503003066641809418      # ID треда, куда отправлять сообщения
ALLOWED_USER_ID = 115475534544109573  # ID единственного разрешённого пользователя

PREFIX = '!s '  # команда для отправки, например: !s Привет всем


@client.event
async def on_ready():
    print(f'Бот запущен как {client.user}')


@client.event
async def on_message(message):
    # Игнорируем сообщения от самого бота
    if message.author == client.user:
        return

    # Проверяем, что писать может только разрешённый пользователь
    if message.author.id != ALLOWED_USER_ID:
        return

    # Проверяем, что сообщение начинается с команды
    if not message.content.startswith(PREFIX):
        return

    text = message.content[len(PREFIX):].strip()
    if not text:
        return

    try:
        # Получаем тред (fetch_channel надёжнее, чем get_channel — 
        # особенно если тред не в кэше бота)
        thread = await client.fetch_channel(THREAD_ID)
        await thread.send(text)

        # Необязательно: подтверждение в исходном канале
        await message.add_reaction('✅')

    except discord.Forbidden:
        await message.channel.send('❌ У бота нет доступа к этому треду.')
    except discord.NotFound:
        await message.channel.send('❌ Тред не найден. Проверь THREAD_ID.')
    except Exception as e:
        await message.channel.send(f'❌ Ошибка: {e}')


client.run(os.environ['DISCORD_TOKEN'])
