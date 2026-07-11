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
# 1) BẢO MẬT TOKEN
# ---------------------------------------------------------------------------
# - Token KHÔNG bao giờ được hardcode trong file .py.
# - load_dotenv() đọc file ".env" (chỉ tồn tại trên máy/máy chủ của bạn).
# - File ".env" PHẢI được liệt kê trong ".gitignore" để không bao giờ bị
#   commit/push lên GitHub. Repo này đi kèm ".gitignore" và ".env.example"
#   (xem 2 file được tạo cùng bot.py) — hãy copy ".env.example" thành ".env"
#   rồi điền token thật vào đó, không commit file ".env".
# - Nếu token từng bị lộ trên GitHub (kể cả trong lịch sử commit cũ), phải
#   vào Discord Developer Portal > Bot > Reset Token ngay lập tức, vì token
#   cũ coi như đã bị xâm phạm dù bạn xoá khỏi code.
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("discipline_bot")

DB_PATH = "progress.db"
CONVERSATION_PATH = "conversation.txt"
GOAL = 3500

forbidden_abbreviations = [
    r'\bko\b', r'\bk\b', r'\bdc\b', r'\bđc\b', r'\br\b',
    r'\bvs\b', r'\bms\b', r'\bh\b', r'\bđx\b', r'\bntn\b'
]

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


# ---------------------------------------------------------------------------
# 2) LƯU TRỮ TIẾN ĐỘ (SQLite + cache RAM, tự lưu/đọc, không mất khi restart)
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
        # Lỗi ghi DB (ví dụ hết dung lượng ổ đĩa) không được phép làm crash
        # bot — cache RAM vẫn đúng, chỉ log lỗi và thử lại ở lần ghi kế tiếp.
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
# 3) QUẢN LÝ CONVERSATION.TXT — AN TOÀN INDEX, KHÔNG BAO GIỜ IndexError
# ---------------------------------------------------------------------------
class ConversationManager:
    """
    Nạp danh sách câu đối thoại từ conversation.txt.
    Dùng index theo kiểu 'quay vòng' (modulo) nên dù tiến độ người dùng vượt
    quá 350 câu (hoặc file chỉ có 1 dòng, hoặc file rỗng/không tồn tại),
    get_line() KHÔNG BAO GIỜ raise IndexError.
    """
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
        # index % len(self.lines) luôn nằm trong khoảng [0, len-1] vì
        # len(self.lines) >= 1 luôn đúng (đảm bảo ở reload()).
        safe_index = index % len(self.lines)
        return self.lines[safe_index]

    def total(self) -> int:
        return len(self.lines)


conversation = ConversationManager(CONVERSATION_PATH)


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


# ---------------------------------------------------------------------------
# HÀM PHỤ TRỢ
# ---------------------------------------------------------------------------
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


def check_message_rules(text: str) -> bool:
    if not text:
        return False
    text = text.strip()
    if not text or not text[0].isupper():
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
# 4) XỬ LÝ LỖI TOÀN CỤC — bot không crash vì tin nhắn lạ / mất kết nối
# ---------------------------------------------------------------------------
@bot.event
async def on_error(event_method, *args, **kwargs):
    # Bắt mọi exception không lường trước xảy ra trong bất kỳ event handler
    # nào (on_message, on_ready, ...). Không làm gì hơn ngoài log, để
    # discord.py tự tiếp tục vòng lặp sự kiện thay vì crash toàn bộ tiến trình.
    logger.error(f"Lỗi không lường trước tại '{event_method}':\n{traceback.format_exc()}")


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return  # bỏ qua lệnh không tồn tại, giống hành vi gốc
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
    # discord.py tự động reconnect (reconnect=True mặc định khi gọi bot.run),
    # đây chỉ là log để bạn theo dõi tình trạng mạng/uptime.
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
            return  # lệnh không xác định, bỏ qua như hành vi gốc

        text = message.content
        user_id = str(message.author.id)

        if check_message_rules(text):
            current_count = store.increment(user_id)

            if current_count < GOAL:
                # Câu đối thoại lấy theo modulo -> an toàn dù vượt quá số
                # dòng có trong conversation.txt (kể cả sau câu thứ 350).
                reply_line = conversation.get_line(current_count - 1)
                await message.channel.send(
                    f"Chính xác! Tiến độ: {current_count}/{GOAL}.\n> {reply_line}"
                )
            else:
                await message.channel.send(
                    f"🎉 CHÚC MỪNG {message.author.mention}! Bạn đã hoàn thành {GOAL}/{GOAL} tin nhắn. Bạn đã được tự do!"
                )
                store.reset(user_id)
        else:
            await message.channel.send(f"{message.author.mention} bạn đã sai! Hãy làm lại và tiếp tục tập luyện.")

    except discord.Forbidden:
        logger.warning(f"Thiếu quyền gửi tin nhắn ở kênh {message.channel.id}")
    except discord.HTTPException:
        logger.exception("Lỗi HTTP khi gửi tin nhắn tới Discord.")
    except Exception:
        # Lưới an toàn cuối cùng: bất kỳ lỗi bất ngờ nào khác (tin nhắn dị
        # dạng, unicode lạ, v.v...) chỉ được log lại, KHÔNG làm crash bot.
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
    await ctx.send(
        f"Bắt đầu nhiệm vụ, {ctx.author.mention}! Chủ đề của bạn là:\n"
        f"> **{selected_topic}**\n\n"
        f"*Nhớ: Viết hoa chữ đầu, có dấu đầy đủ, kết thúc bằng dấu chấm và tuyệt đối không viết tắt.*"
    )


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
