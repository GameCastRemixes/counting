import discord
from discord.ext import commands
from discord import ui
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
# YÊU CẦU: discord.py >= 2.6 (đã test với 2.7.1) — bản cũ hơn KHÔNG có
# discord.ui.LayoutView/Container/Section/TextDisplay/Separator/ActionRow.
#   pip install -U discord.py
#
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

# accent_color của Container — thay thế cho discord.Color của Embed cũ.
COLOR_SUCCESS = 0x57F287
COLOR_ERROR = 0xED4245
COLOR_INFO = 0x5865F2
COLOR_GOLD = 0xFEE75C

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
# HÀM PHỤ TRỢ
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
# ACTION ROW DÙNG CHUNG (buttons persistent — custom_id cố định, sống sót
# qua restart nhờ bot.add_view() ở on_ready)
# ---------------------------------------------------------------------------
class ControlsActionRow(ui.ActionRow):
    @ui.button(
        label="Cố lên! Tôi sẽ làm được!",
        style=discord.ButtonStyle.success,
        emoji="💪",
        custom_id="discipline_cheer_button",
    )
    async def cheer_button(self, interaction: discord.Interaction, button: ui.Button):
        try:
            await interaction.response.send_message(
                "Chỉ còn một chút nữa thôi. Cứ kiên trì gõ từng dòng một nhé!",
                ephemeral=True,
            )
        except discord.HTTPException:
            logger.exception("Không thể phản hồi interaction (có thể đã hết hạn).")

    @ui.button(
        label="Xem quy tắc",
        style=discord.ButtonStyle.secondary,
        emoji="📋",
        custom_id="discipline_rules_button",
    )
    async def rules_button(self, interaction: discord.Interaction, button: ui.Button):
        try:
            await interaction.response.send_message(view=build_rules_view(), ephemeral=True)
        except discord.HTTPException:
            logger.exception("Không thể phản hồi interaction (có thể đã hết hạn).")


class PersistentControlsView(ui.LayoutView):
    """View 'trơ' chỉ chứa ActionRow, đăng ký 1 lần lúc khởi động để các nút
    trên những message cũ vẫn hoạt động sau khi bot restart."""

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ControlsActionRow())


# ---------------------------------------------------------------------------
# COMPONENTS V2 VIEW BUILDERS (thay thế hoàn toàn cho Embed)
# ---------------------------------------------------------------------------
def build_hmd_view(member: discord.Member | discord.User) -> ui.LayoutView:
    tz = datetime.timezone(datetime.timedelta(hours=7))
    ban_start = datetime.datetime(2026, 7, 10, 19, 45, 0, tzinfo=tz)
    ban_end = ban_start + datetime.timedelta(days=7)

    start_ts = int(ban_start.timestamp())
    end_ts = int(ban_end.timestamp())
    current_count = store.get(str(member.id))

    info_section = ui.Section(
        ui.TextDisplay(f"📅 **Bắt đầu**\n<t:{start_ts}:F>\n(<t:{start_ts}:R>)"),
        ui.TextDisplay(f"🔓 **Được tự do**\n<t:{end_ts}:F>\n(<t:{end_ts}:R>)"),
        accessory=ui.Thumbnail(member.display_avatar.url),
    )

    container = ui.Container(
        ui.TextDisplay(f"# ⏳ Contingency Contract\nHồ sơ kỷ luật của {member.mention}"),
        ui.Separator(),
        info_section,
        ui.Separator(),
        ui.TextDisplay(
            f"**✍️ Tiến độ tin nhắn ({current_count}/{GOAL})**\n{build_progress_bar(current_count, GOAL)}"
        ),
        ControlsActionRow(),
        ui.TextDisplay("-# Giữ vững kỷ luật. Sai một câu — reset về 0!"),
        accent_color=COLOR_INFO,
    )

    view = ui.LayoutView(timeout=None)
    view.add_item(container)
    return view


def build_success_view(member: discord.Member | discord.User, current_count: int, reply_line: str) -> ui.LayoutView:
    container = ui.Container(
        ui.TextDisplay(f"## ✅ Chính xác!\n> {reply_line}"),
        ui.Separator(),
        ui.TextDisplay(
            f"**Tiến độ ({current_count}/{GOAL})**\n{build_progress_bar(current_count, GOAL)}"
        ),
        ui.TextDisplay(f"-# {member.display_name} • Tiếp tục phát huy!"),
        accent_color=COLOR_SUCCESS,
    )
    view = ui.LayoutView()
    view.add_item(container)
    return view


def build_mistake_view(member: discord.Member | discord.User, reason: str, previous_count: int) -> ui.LayoutView:
    container = ui.Container(
        ui.TextDisplay(f"## ❌ Vi phạm quy tắc!\n{member.mention} đã mắc lỗi: **{reason}**."),
        ui.Separator(),
        ui.TextDisplay(
            f"**Hậu quả**\nTiến độ đã bị reset về 0 (trước đó: {previous_count}/{GOAL})."
        ),
        ui.TextDisplay(
            "**Ghi nhớ**\n• Viết hoa chữ đầu\n• Có dấu tiếng Việt đầy đủ\n"
            "• Kết thúc bằng dấu chấm\n• Không viết tắt"
        ),
        ui.TextDisplay("-# Đứng dậy và làm lại từ đầu. Kỷ luật là tự do."),
        accent_color=COLOR_ERROR,
    )
    view = ui.LayoutView()
    view.add_item(container)
    return view


def build_completion_view(member: discord.Member | discord.User) -> ui.LayoutView:
    container = ui.Container(
        ui.TextDisplay(
            f"## 🎉 HOÀN THÀNH BẢN ÁN!\n{member.mention} đã hoàn thành **{GOAL}/{GOAL}** "
            f"tin nhắn chuẩn mực. Bạn đã được tự do!"
        ),
        ui.Separator(),
        ui.TextDisplay(f"**Tiến độ cuối cùng**\n{build_progress_bar(GOAL, GOAL)}"),
        ui.TextDisplay("-# Chúc mừng! Đây là thành quả của sự kiên trì."),
        accent_color=COLOR_GOLD,
    )
    view = ui.LayoutView()
    view.add_item(container)
    return view


def build_topic_view(member: discord.Member | discord.User, topic: str) -> ui.LayoutView:
    container = ui.Container(
        ui.TextDisplay(f"## 📝 Nhiệm vụ luyện viết mới\n> **{topic}**"),
        ui.Separator(),
        ui.TextDisplay(
            "**Quy tắc bắt buộc**\n• Viết hoa chữ đầu\n• Có dấu tiếng Việt\n"
            "• Kết thúc bằng dấu chấm\n• Không viết tắt"
        ),
        ui.TextDisplay(f"-# Giao cho {member.display_name} • Sai là reset về 0!"),
        accent_color=COLOR_INFO,
    )
    view = ui.LayoutView()
    view.add_item(container)
    return view


def build_rules_view() -> ui.LayoutView:
    container = ui.Container(
        ui.TextDisplay(
            "## 📋 Quy tắc viết tin nhắn hợp lệ\n"
            "1️⃣ Viết hoa chữ cái đầu câu\n"
            "2️⃣ Có dấu tiếng Việt đầy đủ\n"
            "3️⃣ Kết thúc bằng dấu chấm (.)\n"
            "4️⃣ Không được viết tắt\n\n"
            "⚠️ **Sai bất kỳ quy tắc nào → tiến độ reset về 0.**"
        ),
        accent_color=COLOR_INFO,
    )
    view = ui.LayoutView()
    view.add_item(container)
    return view


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
        bot.add_view(PersistentControlsView())
        logger.info("Đã đăng ký persistent view cho các nút bấm.")
    except Exception:
        logger.exception("Lỗi khi đăng ký persistent view.")

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
                await message.channel.send(view=build_success_view(message.author, current_count, reply_line))
            else:
                store.reset(user_id)
                await message.channel.send(view=build_completion_view(message.author))
        else:
            # Sai quy tắc -> reset tiến độ về 0 ngay lập tức.
            previous_count = store.get(user_id)
            store.reset(user_id)
            await message.channel.send(view=build_mistake_view(message.author, reason, previous_count))

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
    await ctx.send(view=build_hmd_view(ctx.author))


@bot.hybrid_command(name="chude", description="Nhận một chủ đề luyện viết ngẫu nhiên")
async def chude(ctx: commands.Context):
    topics = load_topics()
    selected_topic = random.choice(topics)
    await ctx.send(view=build_topic_view(ctx.author, selected_topic))


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
