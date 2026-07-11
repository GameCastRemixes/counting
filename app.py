import discord
from discord.ext import commands
import re
import random
import os
import sys
import sqlite3
import logging
import traceback
import datetime
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# BẢO MẬT TOKEN — xem .env.example / .gitignore đi kèm.
# ---------------------------------------------------------------------------
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("discipline_bot")

DB_PATH = "progress.db"
CONVERSATION_PATH = "conversation.txt"
GOAL = 3500
PROGRESS_BAR_LENGTH = 20

forbidden_abbreviations = {
    r'\bko\b': "chứa từ viết tắt (ko)",
    r'\bk\b': "chứa từ viết tắt (k)",
    r'\bdc\b': "chứa từ viết tắt (dc)",
    r'\bđc\b': "chứa từ viết tắt (đc)",
    r'\br\b': "chứa từ viết tắt (r)",
    r'\bvs\b': "chứa từ viết tắt (vs)",
    r'\bms\b': "chứa từ viết tắt (ms)",
    r'\bh\b': "chứa từ viết tắt (h)",
    r'\bđx\b': "chứa từ viết tắt (đx)",
    r'\bntn\b': "chứa từ viết tắt (ntn)",
}

# Màu sắc dùng chung cho toàn bộ Embed để giao diện nhất quán.
COLOR_SUCCESS = discord.Color.from_rgb(87, 242, 135)
COLOR_ERROR = discord.Color.from_rgb(237, 66, 69)
COLOR_INFO = discord.Color.from_rgb(88, 101, 242)
COLOR_GOLD = discord.Color.from_rgb(255, 199, 41)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


# ---------------------------------------------------------------------------
# LƯU TRỮ TIẾN ĐỘ (SQLite + cache RAM)
# ---------------------------------------------------------------------------
class ProgressStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        try:
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
        except sqlite3.Error:
            logger.exception("Không thể khởi tạo database, bot sẽ dừng lại.")
            raise

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
        try:
            self.conn.execute(
                """
                INSERT INTO user_progress (user_id, count) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET count = excluded.count
                """,
                (user_id, count),
            )
            self.conn.commit()
        except sqlite3.Error:
            logger.exception(f"Lỗi khi lưu tiến độ cho user {user_id}")


store = ProgressStore(DB_PATH)


# ---------------------------------------------------------------------------
# QUẢN LÝ CONVERSATION.TXT — an toàn index (modulo), không bao giờ crash
# ---------------------------------------------------------------------------
class ConversationManager:
    DEFAULT_LINE = "Tốt lắm, tiếp tục duy trì kỷ luật nhé."

    def __init__(self, path: str):
        self.path = path
        self.lines: list[str] = []
        self.reload()

    def reload(self):
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    self.lines = [line.strip() for line in f if line.strip()]
            else:
                logger.warning(f"Không tìm thấy {self.path}, dùng câu mặc định.")
                self.lines = []
        except OSError:
            logger.exception(f"Lỗi khi đọc {self.path}")
            self.lines = []

        if not self.lines:
            self.lines = [self.DEFAULT_LINE]

    def get_line(self, index: int) -> str:
        safe_index = index % len(self.lines)
        return self.lines[safe_index]

    def total(self) -> int:
        return len(self.lines)


conversation = ConversationManager(CONVERSATION_PATH)


# ---------------------------------------------------------------------------
# HÀM PHỤ TRỢ GIAO DIỆN
# ---------------------------------------------------------------------------
def build_progress_bar(current: int, goal: int, length: int = PROGRESS_BAR_LENGTH) -> str:
    ratio = max(0.0, min(1.0, current / goal))
    filled = round(ratio * length)
    bar = "█" * filled + "░" * (length - filled)
    return f"`{bar}` **{ratio * 100:.1f}%**"


def get_violation_reason(text: str) -> str | None:
    """Trả về lý do vi phạm (tiếng Việt) hoặc None nếu tin nhắn hợp lệ."""
    if not text or not text.strip():
        return "tin nhắn trống"

    stripped = text.strip()
    if not stripped[0].isupper():
        return "chưa viết hoa chữ cái đầu câu"
    if not stripped.endswith('.'):
        return "chưa kết thúc bằng dấu chấm (.)"

    vietnamese_chars = "àáạảãâầấậẩẫăằắặẳẵèéẹẻẽêềếệểễìíịỉĩòóọỏõôồốộổỗơờớợởỡùúụủũưừứựửữỳýỵỷỹđ"
    if not any(c in vietnamese_chars for c in stripped.lower()):
        return "không có dấu tiếng Việt"

    text_lower = stripped.lower()
    for pattern, reason in forbidden_abbreviations.items():
        if re.search(pattern, text_lower):
            return reason

    return None


def check_message_rules(text: str) -> bool:
    return get_violation_reason(text) is None


def load_topics():
    try:
        if os.path.exists("topics.txt"):
            with open("topics.txt", "r", encoding="utf-8") as f:
                topics = [line.strip() for line in f if line.strip()]
                if topics:
                    return topics
    except OSError:
        logger.exception("Lỗi khi đọc topics.txt")
    return ["Hãy viết một đoạn văn ngắn miêu tả cảm xúc của bạn lúc này."]


# ---------------------------------------------------------------------------
# EMBED BUILDERS
# ---------------------------------------------------------------------------
def build_hmd_embed(member: discord.Member | discord.User) -> discord.Embed:
    tz = datetime.timezone(datetime.timedelta(hours=7))
    ban_start = datetime.datetime(2026, 7, 10, 19, 45, 0, tzinfo=tz)
    ban_end = ban_start + datetime.timedelta(days=7)

    start_ts = int(ban_start.timestamp())
    end_ts = int(ban_end.timestamp())
    current_count = store.get(str(member.id))

    embed = discord.Embed(
        title="⏳ Contingency Contract — Tiến độ thi hành án",
        color=COLOR_INFO,
        description=f"Hồ sơ kỷ luật của {member.mention}",
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="📅 Bắt đầu", value=f"<t:{start_ts}:F>\n(<t:{start_ts}:R>)", inline=True)
    embed.add_field(name="🔓 Được tự do", value=f"<t:{end_ts}:F>\n(<t:{end_ts}:R>)", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=False)
    embed.add_field(
        name=f"✍️ Tiến độ tin nhắn ({current_count}/{GOAL})",
        value=build_progress_bar(current_count, GOAL),
        inline=False,
    )
    embed.set_footer(text="Giữ vững kỷ luật. Sai một câu — reset về 0!")
    return embed


def build_success_embed(member: discord.Member | discord.User, current_count: int, reply_line: str) -> discord.Embed:
    embed = discord.Embed(
        title="✅ Chính xác!",
        color=COLOR_SUCCESS,
        description=f"> {reply_line}",
    )
    embed.add_field(
        name=f"Tiến độ ({current_count}/{GOAL})",
        value=build_progress_bar(current_count, GOAL),
        inline=False,
    )
    embed.set_footer(text=f"{member.display_name} • Tiếp tục phát huy!")
    return embed


def build_mistake_embed(member: discord.Member | discord.User, reason: str, previous_count: int) -> discord.Embed:
    embed = discord.Embed(
        title="❌ Vi phạm quy tắc!",
        color=COLOR_ERROR,
        description=f"{member.mention} đã mắc lỗi: **{reason}**.",
    )
    embed.add_field(
        name="Hậu quả",
        value=f"Tiến độ đã bị **reset về 0** (trước đó: {previous_count}/{GOAL}).",
        inline=False,
    )
    embed.add_field(
        name="Ghi nhớ",
        value="• Viết hoa chữ đầu\n• Có dấu tiếng Việt đầy đủ\n• Kết thúc bằng dấu chấm\n• Không viết tắt",
        inline=False,
    )
    embed.set_footer(text="Đứng dậy và làm lại từ đầu. Kỷ luật là tự do.")
    return embed


def build_completion_embed(member: discord.Member | discord.User) -> discord.Embed:
    embed = discord.Embed(
        title="🎉 HOÀN THÀNH BẢN ÁN!",
        color=COLOR_GOLD,
        description=f"{member.mention} đã hoàn thành **{GOAL}/{GOAL}** tin nhắn chuẩn mực. Bạn đã được tự do!",
    )
    embed.add_field(name="Tiến độ cuối cùng", value=build_progress_bar(GOAL, GOAL), inline=False)
    embed.set_footer(text="Chúc mừng! Đây là thành quả của sự kiên trì.")
    return embed


def build_topic_embed(member: discord.Member | discord.User, topic: str) -> discord.Embed:
    embed = discord.Embed(
        title="📝 Nhiệm vụ luyện viết mới",
        color=COLOR_INFO,
        description=f"> **{topic}**",
    )
    embed.add_field(
        name="Quy tắc bắt buộc",
        value="• Viết hoa chữ đầu\n• Có dấu tiếng Việt\n• Kết thúc bằng dấu chấm\n• Không viết tắt",
        inline=False,
    )
    embed.set_footer(text=f"Giao cho {member.display_name} • Sai là reset về 0!")
    return embed


# ---------------------------------------------------------------------------
# UI COMPONENTS
# ---------------------------------------------------------------------------
class BanStatusView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Cố lên! Tôi sẽ làm được!", style=discord.ButtonStyle.success, emoji="💪")
    async def cheer_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await interaction.response.send_message(
                "Chỉ còn một chút nữa thôi. Cứ kiên trì gõ từng dòng một nhé!",
                ephemeral=True,
            )
        except discord.HTTPException:
            logger.exception("Không thể phản hồi interaction (có thể đã hết hạn).")

    @discord.ui.button(label="Xem quy tắc", style=discord.ButtonStyle.secondary, emoji="📋")
    async def rules_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        rules_embed = discord.Embed(
            title="📋 Quy tắc viết tin nhắn hợp lệ",
            color=COLOR_INFO,
            description=(
                "1️⃣ Viết hoa chữ cái đầu câu\n"
                "2️⃣ Có dấu tiếng Việt đầy đủ\n"
                "3️⃣ Kết thúc bằng dấu chấm (.)\n"
                "4️⃣ Không được viết tắt\n\n"
                "⚠️ **Sai bất kỳ quy tắc nào → tiến độ reset về 0.**"
            ),
        )
        try:
            await interaction.response.send_message(embed=rules_embed, ephemeral=True)
        except discord.HTTPException:
            logger.exception("Không thể phản hồi interaction (có thể đã hết hạn).")


# ---------------------------------------------------------------------------
# XỬ LÝ LỖI TOÀN CỤC
# ---------------------------------------------------------------------------
@bot.event
async def on_error(event_method, *args, **kwargs):
    logger.error(f"Lỗi không lường trước tại '{event_method}':\n{traceback.format_exc()}")


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"Từ từ đã! Thử lại sau {error.retry_after:.1f}s.")
        return
    logger.error(f"Lỗi khi chạy lệnh '{ctx.command}': {error}")
    try:
        await ctx.send("Có lỗi xảy ra khi thực hiện lệnh này, mình đã ghi log lại rồi.")
    except discord.HTTPException:
        pass


@bot.event
async def on_disconnect():
    logger.warning("Bot mất kết nối tới Discord, đang chờ tự động kết nối lại...")


@bot.event
async def on_resumed():
    logger.info("Đã kết nối lại với Discord thành công.")


# ---------------------------------------------------------------------------
# SỰ KIỆN CHÍNH
# ---------------------------------------------------------------------------
@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        logger.info(f"Đã đồng bộ {len(synced)} slash command.")
    except discord.HTTPException:
        logger.exception("Lỗi khi đồng bộ slash command.")
    logger.info(f"Bot {bot.user} đã sẵn sàng phục vụ và Token đã được bảo mật!")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    try:
        ctx = await bot.get_context(message)
        if ctx.valid:
            await bot.process_commands(message)
            return

        if message.content.startswith('!'):
            return

        text = message.content
        user_id = str(message.author.id)
        reason = get_violation_reason(text)

        if reason is None:
            current_count = store.increment(user_id)

            if current_count < GOAL:
                reply_line = conversation.get_line(current_count - 1)
                embed = build_success_embed(message.author, current_count, reply_line)
                await message.channel.send(embed=embed)
            else:
                store.reset(user_id)
                embed = build_completion_embed(message.author)
                await message.channel.send(embed=embed)
        else:
            # Sai quy tắc -> reset tiến độ về 0 ngay lập tức.
            previous_count = store.get(user_id)
            store.reset(user_id)
            embed = build_mistake_embed(message.author, reason, previous_count)
            await message.channel.send(embed=embed)

    except discord.Forbidden:
        logger.warning(f"Thiếu quyền gửi tin nhắn ở kênh {message.channel.id}")
    except discord.HTTPException:
        logger.exception("Lỗi HTTP khi gửi tin nhắn tới Discord.")
    except Exception:
        logger.error(f"Lỗi không lường trước khi xử lý tin nhắn:\n{traceback.format_exc()}")


# ---------------------------------------------------------------------------
# LỆNH (hybrid = dùng được cả "!lệnh" lẫn "/lệnh")
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
    embed = build_topic_embed(ctx.author, selected_topic)
    await ctx.send(embed=embed)


# ---------------------------------------------------------------------------
# CHẠY BOT
# ---------------------------------------------------------------------------
def main():
    token = os.getenv('DISCORD_BOT_TOKEN')
    if not token:
        logger.critical("LỖI: Không tìm thấy DISCORD_BOT_TOKEN! Kiểm tra lại file .env.")
        sys.exit(1)

    try:
        bot.run(token, log_handler=None)
    except discord.LoginFailure:
        logger.critical("Token không hợp lệ. Kiểm tra lại giá trị DISCORD_BOT_TOKEN trong .env.")
        sys.exit(1)
    except Exception:
        logger.critical(f"Bot dừng do lỗi nghiêm trọng:\n{traceback.format_exc()}")
        sys.exit(1)


if __name__ == "__main__":
    main()
