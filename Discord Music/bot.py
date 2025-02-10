import discord
import os
import yt_dlp
import asyncio
import json
from discord.ext import commands
from dotenv import load_dotenv

############################################################################################################
#                                                                                                          #
#                                                  SET UP                                                  #
#                                                                                                          #
############################################################################################################

load_dotenv()
TOKEN = os.getenv('BOT_TOKEN')

os.environ["PATH"] += os.pathsep + r"D:\Bot\Discord\Music\ffmpeg-7.1-full_build\bin"

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="?", intents=intents)

voice_clients = {}
queues = {}

yt_dl_options = {
    "format": "bestaudio[ext=m4a]/bestaudio/best",
    "noplaylist": False
}
ytdl = yt_dlp.YoutubeDL(yt_dl_options)

ffmpeg_options = {'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5','options': '-vn -filter:a "volume=0.25"'}

def load_songs():
    try:
        with open("songs.json", "r", encoding="utf-8") as file:
            return json.load(file)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    
def save_songs():
    """Lưu danh sách bài hát vào songs.json"""
    with open("songs.json", "w", encoding="utf-8") as file:
        json.dump(songs, file, ensure_ascii=False, indent=4)

songs = load_songs()

############################################################################################################
#                                                                                                          #
#                                                PHÁT NHẠC                                                 #
#                                                                                                          # 
############################################################################################################

@bot.event
async def on_ready():
    print(f'{bot.user} is now jamming!')

async def play_next(ctx):
    """Phát bài hát tiếp theo trong queue nếu có."""
    guild_id = ctx.guild.id
    if guild_id in queues and queues[guild_id]: 
        next_url = queues[guild_id].pop(0)
        await play(ctx, next_url, from_queue=True)
        
@bot.command(name="list_songs")
async def list_songs(ctx):
    """Hiển thị danh sách bài hát có sẵn."""
    if not songs:
        await ctx.send("📂 Không có bài hát nào trong danh sách!")
        return

    song_list = "\n".join([f"{i+1}. {name}" for i, name in enumerate(songs.keys())])
    await ctx.send(f"# 🎶 Danh sách bài hát:\n{song_list}")
    
@bot.command(name="add_song")
async def add_song(ctx, name: str, url: str):
    """Thêm bài hát vào danh sách"""
    if name in songs:
        await ctx.send(f"❌ Bài hát **{name}** đã có trong danh sách!")
        return

    songs[name] = url
    save_songs()
    await ctx.send(f"✅ Đã thêm bài hát **{name}** vào danh sách!")
    
@bot.command(name="delete_song")
async def delete_song(ctx, name: str):
    """Xóa bài hát khỏi danh sách"""
    if name not in songs:
        await ctx.send(f"❌ Không tìm thấy bài hát **{name}** trong danh sách!")
        return

    del songs[name]
    save_songs()
    await ctx.send(f"🗑 Đã xóa bài hát **{name}** khỏi danh sách!")

@bot.command(name="play")
async def play(ctx, url: str, from_queue=False):
    """Phát nhạc từ YouTube."""
    try:
        voice_client = ctx.guild.voice_client
        if not voice_client or not voice_client.is_connected():
            voice_client = await ctx.author.voice.channel.connect()
            voice_clients[ctx.guild.id] = voice_client

        if voice_client.is_playing() and not from_queue:
            if ctx.guild.id not in queues:
                queues[ctx.guild.id] = []
            queues[ctx.guild.id].append(url)
            await ctx.send("🎶 Đã thêm vào hàng đợi!")
            return

        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=False))
        
        if "entries" in data: 
            for entry in data["entries"]:
                queues.setdefault(ctx.guild.id, []).append(entry["url"])

            await ctx.send(f"📜 Đã thêm {len(data['entries'])} bài hát từ danh sách phát vào hàng đợi!")
            if not voice_client.is_playing():
                await play_next(ctx)
        else:
            song = data['url']
            player = discord.FFmpegOpusAudio(song, **ffmpeg_options)

            voice_clients[ctx.guild.id].play(player, after=lambda _: bot.loop.create_task(play_next(ctx)))
            await ctx.send(f"🎵 Đang phát: {data['title']}")

    except Exception as e:
        print(e)
        await ctx.send("❌ Không thể phát nhạc!")
        
@bot.command(name="play_name")
async def play_name(ctx, *song_name):
    """Phát nhạc theo tên từ danh sách có sẵn."""
    song_name = " ".join(song_name)  
    if song_name in songs:
        await play(ctx, songs[song_name])
    else:
        await ctx.send("❌ Không tìm thấy bài hát trong danh sách!")

@bot.command(name="pause")
async def pause(ctx):
    """Tạm dừng nhạc."""
    voice_client = ctx.guild.voice_client
    if voice_client and voice_client.is_playing():
        voice_client.pause()
        await ctx.send("⏸ Nhạc đã bị tạm dừng.")

@bot.command(name="resume")
async def resume(ctx):
    """Tiếp tục phát nhạc."""
    voice_client = ctx.guild.voice_client
    if voice_client and voice_client.is_paused():
        voice_client.resume()
        await ctx.send("▶ Tiếp tục phát nhạc.")

@bot.command(name="stop")
async def stop(ctx):
    """Dừng nhạc và ngắt kết nối."""
    guild_id = ctx.guild.id
    if guild_id in queues:
        queues[guild_id].clear()
    voice_client = ctx.guild.voice_client
    if voice_client and voice_client.is_connected():
        voice_client.stop()
        await voice_client.disconnect()
        await ctx.send("⏹ Đã dừng nhạc và thoát khỏi kênh voice.")

@bot.command(name="skip")
async def skip(ctx):
    """Bỏ qua bài hát hiện tại và phát bài tiếp theo."""
    voice_client = ctx.guild.voice_client
    if voice_client and voice_client.is_playing():
        voice_client.stop()  
        await play_next(ctx) 
        await ctx.send("⏭ Đã bỏ qua bài hát!")
    else:
        await ctx.send("❌ Không có bài hát nào đang phát.")

@bot.command(name="help_me")
async def help_me(ctx):
    """Hiển thị danh sách lệnh hiện có."""
    commands_list = (
        "# 🎵 Danh sách lệnh:\n"
        "?list_songs - In ra danh sách các bài nhạc đã lưu\n"
        "?add_song \"<name>\" \"<url>\" - Lưu bài hát mới vào danh sách\n"
        "?delete_song \"<name>\" - Xóa một bài hát trong danh sách\n"
        "?play <url> - Phát nhạc từ YouTube\n"
        "?play_name <tên bài> - Phát nhạc theo tên từ danh sách có sẵn\n"
        "?pause - Tạm dừng nhạc\n"
        "?resume - Tiếp tục phát nhạc\n"
        "?stop - Dừng nhạc và thoát khỏi kênh voice\n"
        "?skip - Bỏ qua bài hát hiện tại nhưng phát lại sau\n"
        "?help_me - Hiển thị danh sách lệnh"
    )
    await ctx.send(commands_list)

############################################################################################################
#                                                                                                          #
#                                                 RUN BOT                                                  #
#                                                                                                          #
############################################################################################################

bot.run(TOKEN)
