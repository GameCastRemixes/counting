import discord
from discord.ext import commands
from discord import app_commands
import re
import random
import os
import sqlite3
import datetime
from dotenv import load_dotenv

# Tải các biến môi trường từ file .env
load_dotenv()

DB_PATH = "progress.db"
GOAL = 3500

forbidden_abbreviations = [
    r'\bko\b', r'\bk\b', r'\bdc\b', r'\bđc\b', r'\br\b',
    r'\bvs\b', r'\bms\b', r'\bh\b', r'\bđx\b', r'\bntn\b'
]

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ---------------------------------------------------------------------------
# LỚP LƯU TRỮ (SQLite + cache RAM để đọc/ghi nhanh, tránh query mỗi tin nhắn)
# ---------------------------------------------------------------------------
class ProgressStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_progress (
                user_id TEXT PRIMARY KEY,
                count INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self.conn.commit()
        # Cache toàn bộ vào RAM khi khởi động — đọc cực nhanh, không cần
        # query DB cho mỗi tin nhắn tới.
        self.cache: dict[str, int] = {}
        for row in self.conn.execute("SELECT user_id, count FROM user_progress"):
            self.cache[row[0]] = row[1]

    def get(self, user_id: str) -> int:
        return self.cache.get(user_id, 0)

    def increment(self, user_id: str) -> int:
        new_count = self.cache.get(user_id, 0) + 1
        self.cache[user_id] = new_count
        self._persist(user_id, new_count)
        return new_count

    def reset(self, user_id: str):
        self.cache[user_id] = 0
        self._persist(user_id, 0)

    def _persist(self, user_id: str, count: int):
        self.conn.execute(
            """
            INSERT INTO user_progress (user_id, count) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET count = excluded.count
            """,
            (user_id, count),
        )
        self.conn.commit()


store = ProgressStore(DB_PATH)

# ---------------------------------------------------------------------------
# UI COMPONENTS
# ---------------------------------------------------------------------------
class BanStatusView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Cố lên! Tôi sẽ làm được!", style=discord.ButtonStyle.success, emoji="💪")
    async def cheer_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Chỉ còn một chút nữa thôi. Cứ kiên trì gõ từng dòng một nhé!",
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# HÀM PHỤ TRỢ (logic kiểm tra giữ nguyên 100%)
# ---------------------------------------------------------------------------
def load_topics():
    if os.path.exists("topics.txt"):
        with open("topics.txt", "r", encoding="utf-8") as f:
            topics = [line.strip() for line in f if line.strip()]
            if topics:
                return topics
    return ["Hãy viết một đoạn văn ngắn miêu tả cảm xúc của bạn lúc này."]


def check_message_rules(text: str) -> bool:
    if not text:
        return False
    text = text.strip()
    if not text[0].isupper():
        return False
    if not text.endswith('.'):
        return False

    vietnamese_chars = "àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ"
    if not any(c in vietnamese_chars for c in text.lower()):
        return False

    text_lower = text.lower()
    for abbr in forbidden_abbreviations:
        if re.search(abbr, text_lower):
            return False
    return True


def build_hmd_embed(member: discord.Member | discord.User) -> discord.Embed:
    tz = datetime.timezone(datetime.timedelta(hours=7))
    ban_start = datetime.datetime(2026, 7, 10, 19, 45, 0, tzinfo=tz)
    ban_end = ban_start + datetime.timedelta(days=7)

    start_ts = int(ban_start.timestamp())
    end_ts = int(ban_end.timestamp())

    embed = discord.Embed(
        title="⏳ Tiến độ Contingency Contract",
        color=discord.Color.brand_red(),
        description=f"Dữ liệu án phạt của {member.mention}",
    )
    embed.add_field(name="Bắt đầu từ:", value=f"<t:{start_ts}:F>\n(<t:{start_ts}:R>)", inline=False)
    embed.add_field(name="Được tự do vào:", value=f"<t:{end_ts}:F>\n(<t:{end_ts}:R>)", inline=False)

    current_count = store.get(str(member.id))
    embed.add_field(name="Tiến độ tin nhắn:", value=f"**{current_count} / {GOAL}** tin nhắn chuẩn mực.", inline=False)
    embed.set_footer(text="Giữ vững kỷ luật. Không được viết tắt!")
    return embed


# ---------------------------------------------------------------------------
# SỰ KIỆN
# ---------------------------------------------------------------------------
@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"Đã đồng bộ {len(synced)} slash command.")
    except Exception as e:
        print(f"Lỗi đồng bộ slash command: {e}")
    print(f"Bot {bot.user} đã sẵn sàng phục vụ và Token đã được bảo mật!")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Nếu đây là một lệnh hợp lệ (!hmd, !chude, ...) -> để commands.Bot xử lý,
    # không chạy tiếp logic chấm điểm tin nhắn bên dưới.
    ctx = await bot.get_context(message)
    if ctx.valid:
        await bot.process_commands(message)
        return

    # Không phải lệnh nhưng bắt đầu bằng "!" (lệnh không tồn tại) -> bỏ qua,
    # giống hành vi gốc.
    if message.content.startswith('!'):
        return

    text = message.content
    user_id = str(message.author.id)

    if check_message_rules(text):
        current_count = store.increment(user_id)

        if current_count < GOAL:
            await message.channel.send(f"Chính xác! Tiến độ: {current_count}/{GOAL}.")
        else:
            await message.channel.send(
                f"🎉 CHÚC MỪNG {message.author.mention}! Bạn đã hoàn thành {GOAL}/{GOAL} tin nhắn. Bạn đã được tự do!"
            )
            store.reset(user_id)
    else:
        await message.channel.send(f"{message.author.mention} bạn đã sai! Hãy làm lại và tiếp tục tập luyện.")


# ---------------------------------------------------------------------------
# LỆNH (hybrid = dùng được cả dạng "!lệnh" lẫn "/lệnh")
# ---------------------------------------------------------------------------
@bot.hybrid_command(name="hmd", aliases=["howmanydayspassed"], description="Xem tiến độ án phạt Contingency Contract")
async def hmd(ctx: commands.Context):
    embed = build_hmd_embed(ctx.author)
    view = BanStatusView()
    await ctx.send(embed=embed, view=view)


@bot.hybrid_command(name="chude", description="Nhận một chủ đề luyện viết ngẫu nhiên")
async def chude(ctx: commands.Context):
    topics = load_topics()
    selected_topic = random.choice(topics)
    await ctx.send(
        f"Bắt đầu nhiệm vụ, {ctx.author.mention}! Chủ đề của bạn là:\n"
        f"> **{selected_topic}**\n\n"
        f"*Nhớ: Viết hoa chữ đầu, có dấu đầy đủ, kết thúc bằng dấu chấm và tuyệt đối không viết tắt.*"
    )


# ---------------------------------------------------------------------------
# CHẠY BOT
# ---------------------------------------------------------------------------
TOKEN = os.getenv('DISCORD_BOT_TOKEN')

if TOKEN is None:
    print("LỖI: Không tìm thấy Token! Vui lòng kiểm tra lại file .env")
else:
    bot.run(TOKEN)
