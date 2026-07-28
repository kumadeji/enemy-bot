import discord
import os

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)

CHANNEL_ID = 734494109590487043  # ID нужного канала

@client.event
async def on_ready():
    print(f'Бот запущен как {client.user}')

@client.event
async def on_message(message):
    if message.author == client.user:
        return
    
    if message.channel.id == CHANNEL_ID:
        await message.channel.send(f'Ты написал: {message.content}')

client.run(os.environ['DISCORD_TOKEN'])
