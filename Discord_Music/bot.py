import discord
import os
import yt_dlp
import asyncio
import json
import sys
from discord.ext import commands
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler

############################################################################################################
#                                                                                                          #
#                                                  SET UP                                                  #
#                                                                                                          #
############################################################################################################

load_dotenv()
TOKEN = os.getenv('BOT_TOKEN')

os.environ["PATH"] += os.pathsep + r"D:\Bot\Discord\Music\ffmpeg-7.1-full_build\bin"

# Kích hoạt intents cần thiết bao gồm member để auto-role và đổi nickname
intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # Bật intents thành viên
bot = commands.Bot(command_prefix="?", intents=intents)

voice_clients = {}
queues = {}

yt_dl_options = {
    "format": "bestaudio/best",
    "noplaylist": False,  
    "default_search": "ytsearch",  
    "source_address": "0.0.0.0",
    "postprocessors": [{
        "key": "FFmpegExtractAudio",
        "preferredcodec": "mp3",
        "preferredquality": "192",
    }]
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
#                                             KHỞI ĐỘNG LẠI                                                #
#                                                                                                          # 
############################################################################################################

@bot.command(name="restart")
async def restart(ctx):
    """Chờ bài hát hiện tại phát xong rồi khởi động lại bot."""
    voice_client = ctx.guild.voice_client

    if voice_client and voice_client.is_playing():
        await ctx.send("🔄 Bot đang phát nhạc. Sẽ khởi động lại sau khi bài hát kết thúc...")

        while voice_client and voice_client.is_playing():
            await asyncio.sleep(1)

    await ctx.send("🔄 Đang khởi động lại bot...")
    os.execv(sys.executable, [sys.executable] + sys.argv)

############################################################################################################
#                                                                                                          #
#                                        CHỈNH SỬA DANH SÁCH NHẠC                                          #
#                                                                                                          # 
############################################################################################################

@bot.event
async def on_ready():
    print(f'{bot.user} is now jamming!')
    
    scheduler = AsyncIOScheduler()
    scheduler.add_job(lambda: os.execv(sys.executable, [sys.executable] + sys.argv), 'cron', hour=0, minute=0)
    scheduler.start()

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
async def delete_song(ctx, *, name: str):
    """Xóa bài hát khỏi danh sách"""
    if name not in songs:
        await ctx.send(f"❌ Không tìm thấy bài hát **{name}** trong danh sách!")
        return

    del songs[name]
    save_songs()
    await ctx.send(f"🗑 Đã xóa bài hát **{name}** khỏi danh sách!")
    
############################################################################################################
#                                                                                                          #
#                                                PHÁT NHẠC                                                 #
#                                                                                                          # 
############################################################################################################

@bot.command(name="play")
async def play(ctx, url: str, from_queue=False):
    """Phát nhạc từ YouTube, Spotify, SoundCloud."""
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
            
    except discord.HTTPException:
        await ctx.send("❌ Mạng bị gián đoạn, thử lại sau!")

    except Exception as e:
        print(e)
        await ctx.send("❌ Không thể phát nhạc!")
        
@bot.command(name="play_all")
async def play_all(ctx):
    """Phát toàn bộ danh sách nhạc đã lưu."""
    if not songs:
        await ctx.send("📂 Không có bài hát nào trong danh sách!")
        return

    guild_id = ctx.guild.id
    if guild_id not in queues:
        queues[guild_id] = []

    song_urls = list(songs.values())
    
    if ctx.guild.voice_client and ctx.guild.voice_client.is_playing():
        queues[guild_id].extend(song_urls)
        await ctx.send(f"🎶 Đã thêm {len(song_urls)} bài hát vào hàng đợi!")
    else:
        queues[guild_id].extend(song_urls[1:])  
        await play(ctx, song_urls[0])  
        
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

############################################################################################################
#                                                                                                          #
#                                             XỬ LÝ THÀNH VIÊN MỚI                                            #
#                                                                                                          #
############################################################################################################

# Tên role và prefix cho nickname
AUTO_ROLE_NAME = "Dân thường"
NICK_PREFIX = "[Dân thường] "

@bot.event
async def on_member_join(member: discord.Member):
    """
    Sự kiện khi thành viên mới join: gán role và đổi nickname.
    """
    guild = member.guild
    role = discord.utils.get(guild.roles, name=AUTO_ROLE_NAME)

    # Gán role nếu có
    if role:
        try:
            await member.add_roles(role)
            print(f"Gán role '{AUTO_ROLE_NAME}' cho {member.name}")
        except discord.Forbidden:
            print("Bot không có quyền gán vai trò.")

    # Đổi nickname
    try:
        # Ưu tiên lấy Global Name (hiển thị chính thức), fallback về username
        display_name = member.global_name or member.name
        new_nick = f"[Dân thường] {display_name}"
        await member.edit(nick=new_nick)
        print(f"Đã đổi nickname của {member.name} thành {new_nick}")
    except discord.Forbidden:
        print("Bot không có quyền đổi nickname.")
    except Exception as e:
        print(f"Lỗi đổi nickname: {e}")
        
############################################################################################################
#                                                                                                          #
#                                                 HỖ TRỢ                                                   #
#                                                                                                          #
############################################################################################################

@bot.command(name="help_me")
async def help_me(ctx):
    """Hiển thị danh sách lệnh hiện có."""
    help_message = """
# 🎵 Danh sách các lệnh của bot:
- `?list_songs` : In ra danh sách các bài nhạc đã lưu.
- `?add_song "<name>" "<url>"` : Lưu bài hát mới vào danh sách.
- `?delete_song <name>` : Xóa một bài hát trong danh sách.
- `?play <url>` : Phát nhạc từ YouTube.
- `?play_all` : Phát tất cả nhạc trong danh sách.
- `?play_name <tên bài>` : Phát nhạc theo tên từ danh sách có sẵn.
- `?pause` : Tạm dừng nhạc.
- `?resume` : Tiếp tục phát nhạc.
- `?stop` : Dừng nhạc và thoát khỏi kênh voice.
- `?skip` : Bỏ qua bài hát hiện tại nhưng phát lại sau.
- `?restart` : Khởi động lại bot.
- `?help_me` : Hiển thị danh sách lệnh.
"""
    await ctx.send(help_message)

############################################################################################################
#                                                                                                          #
#                                                 RUN BOT                                                  #
#                                                                                                          #
############################################################################################################

bot.run(TOKEN)
