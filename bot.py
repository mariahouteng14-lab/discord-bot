"""
DQ10 コミュニティ用 パーティ募集Bot

機能:
- /募集 コマンドで募集を作成(スレッド付き)
- 対象ロールを複数選択可(未選択なら全員に募集 = @everyone)
- ロール指定ありの募集は、ロール外の人の参加を禁止(エラー表示)
- 管理者ロールには全ての募集で必ずメンション
- ボタンで 参加 / 取り消し / 募集締切
- 残り枠・参加者一覧をリアルタイム更新
- 満員通知・開始15分前リマインド・開始時刻で自動締切
- SQLiteに保存するので再起動してもボタンが動く
"""
import json
import logging
import os
import re
import sqlite3
import time
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("recruit-bot")

# ---------------------------------------------------------------
# 設定(.env から読み込み)
# ---------------------------------------------------------------
TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = int(os.environ.get("GUILD_ID", "0"))
DB_PATH = os.environ.get("DB_PATH", "recruit.db")

ADMIN_ROLE_ID = int(os.environ["ADMIN_ROLE_ID"])

# 募集の対象として選べるロール(表示名: ロールID)
TARGET_ROLES = {
    "エキスパート": int(os.environ["EXPERT_ROLE_ID"]),
    "TA勢": int(os.environ["TA_ROLE_ID"]),
    "勝ち終わ勢": int(os.environ["KACHIOWA_ROLE_ID"]),
    "初心者": int(os.environ["BEGINNER_ROLE_ID"]),
}
ID_TO_LABEL = {v: k for k, v in TARGET_ROLES.items()}

JST = ZoneInfo("Asia/Tokyo")
REMIND_BEFORE_SEC = 15 * 60  # 開始何秒前にリマインドするか

# ---------------------------------------------------------------
# データベース
# ---------------------------------------------------------------
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript(
    """
CREATE TABLE IF NOT EXISTS recruits (
    message_id    INTEGER PRIMARY KEY,
    channel_id    INTEGER NOT NULL,
    thread_id     INTEGER,
    owner_id      INTEGER NOT NULL,
    title         TEXT NOT NULL,
    note          TEXT,
    start_ts      INTEGER NOT NULL,
    capacity      INTEGER NOT NULL,
    roles         TEXT NOT NULL DEFAULT '[]',
    status        TEXT NOT NULL DEFAULT 'open',
    reminded      INTEGER NOT NULL DEFAULT 0,
    full_notified INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS participants (
    message_id INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    joined_at  INTEGER NOT NULL,
    PRIMARY KEY (message_id, user_id)
);
"""
)
db.commit()


def get_recruit(message_id: int):
    return db.execute(
        "SELECT * FROM recruits WHERE message_id = ?", (message_id,)
    ).fetchone()


def get_members(message_id: int) -> list[int]:
    rows = db.execute(
        "SELECT user_id FROM participants WHERE message_id = ? ORDER BY rowid",
        (message_id,),
    ).fetchall()
    return [r["user_id"] for r in rows]


# ---------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------
def is_admin(member: discord.Member) -> bool:
    return member.guild_permissions.administrator or any(
        r.id == ADMIN_ROLE_ID for r in member.roles
    )


def is_allowed(member: discord.Member, role_ids: list[int]) -> bool:
    """対象ロール指定なし→誰でもOK / 指定あり→該当ロール保持者と管理者のみ"""
    if not role_ids:
        return True
    if is_admin(member):
        return True
    return any(r.id in role_ids for r in member.roles)


_DATE_PATTERN = re.compile(
    r"^(?:(\d{4})[/\-.])?(\d{1,2})[/\-.](\d{1,2})\s+(\d{1,2}):(\d{2})$"
)


def parse_start(text: str) -> datetime | None:
    """'11/15 21:00' や '2026/11/15 21:00' を日本時間の datetime に変換"""
    text = unicodedata.normalize("NFKC", text).strip()
    m = _DATE_PATTERN.match(text)
    if not m:
        return None
    y, mo, d, h, mi = m.groups()
    now = datetime.now(JST)
    year = int(y) if y else now.year
    try:
        dt = datetime(year, int(mo), int(d), int(h), int(mi), tzinfo=JST)
        # 年省略で半年以上前になる場合は来年として扱う(12月に1月の日付を入力した時など)
        if not y and (now - dt).days > 180:
            dt = dt.replace(year=year + 1)
    except ValueError:
        return None
    return dt


async def fetch_channel(channel_id: int):
    return bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)


def build_embed(r, members: list[int]) -> discord.Embed:
    cap = r["capacity"]
    left = max(cap - len(members), 0)
    if r["status"] == "closed":
        state, color = "🔒 募集終了", 0x95A5A6
    elif left == 0:
        state, color = "✅ 満員", 0x2ECC71
    else:
        state, color = "🟢 募集中", 0x3498DB

    e = discord.Embed(
        title=f"🎮 {r['title']}",
        description=r["note"] or None,
        color=color,
    )
    ts = r["start_ts"]
    e.add_field(name="状態", value=state, inline=True)
    e.add_field(name="残り枠", value=f"{left} / {cap}", inline=True)
    e.add_field(name="募集主", value=f"<@{r['owner_id']}>", inline=True)
    e.add_field(name="開始日時", value=f"<t:{ts}:F>\n(<t:{ts}:R>)", inline=False)

    roles = json.loads(r["roles"])
    target = " ".join(f"<@&{i}>" for i in roles) if roles else "全員"
    e.add_field(name="対象", value=target, inline=False)

    lines = []
    for i, uid in enumerate(members, 1):
        crown = " 👑" if uid == r["owner_id"] else ""
        lines.append(f"{i}. <@{uid}>{crown}")
    for i in range(len(members) + 1, cap + 1):
        lines.append(f"{i}. (空き)")
    e.add_field(name="参加者", value="\n".join(lines), inline=False)
    return e


async def send_to_thread(r, text: str):
    """募集スレッドにメッセージを送る(失敗してもBotは止めない)"""
    try:
        if r["thread_id"]:
            thread = await fetch_channel(r["thread_id"])
            await thread.send(text)
    except Exception:
        log.exception("スレッドへの送信に失敗")


async def finalize(r, thread_text: str):
    """募集を締め切り、メッセージを更新する"""
    mid = r["message_id"]
    db.execute("UPDATE recruits SET status = 'closed' WHERE message_id = ?", (mid,))
    db.commit()
    r = get_recruit(mid)
    members = get_members(mid)
    try:
        channel = await fetch_channel(r["channel_id"])
        await channel.get_partial_message(mid).edit(
            embed=build_embed(r, members), view=RecruitView(closed=True)
        )
    except Exception:
        log.exception("募集メッセージの更新に失敗")
    mentions = " ".join(f"<@{m}>" for m in members)
    await send_to_thread(r, f"{thread_text}\n{mentions}".strip())


# ---------------------------------------------------------------
# 募集メッセージのボタン(Bot再起動後も動く永続View)
# ---------------------------------------------------------------
class RecruitView(discord.ui.View):
    def __init__(self, closed: bool = False):
        super().__init__(timeout=None)
        if closed:
            for child in self.children:
                child.disabled = True

    @discord.ui.button(
        label="参加する", style=discord.ButtonStyle.success, emoji="✋",
        custom_id="recruit:join",
    )
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        mid = interaction.message.id
        r = get_recruit(mid)
        if r is None:
            return await interaction.response.send_message(
                "この募集のデータが見つかりません。", ephemeral=True
            )
        if r["status"] != "open":
            return await interaction.response.send_message(
                "この募集はすでに終了しています。", ephemeral=True
            )

        roles = json.loads(r["roles"])
        if not is_allowed(interaction.user, roles):
            names = "、".join(ID_TO_LABEL.get(i, "指定ロール") for i in roles)
            return await interaction.response.send_message(
                f"❌ この募集は **{names}** 限定です。\n"
                "対象ロールをお持ちでないため参加できません。",
                ephemeral=True,
            )

        members = get_members(mid)
        if interaction.user.id in members:
            return await interaction.response.send_message(
                "すでに参加しています。", ephemeral=True
            )
        if len(members) >= r["capacity"]:
            return await interaction.response.send_message(
                "満員です。", ephemeral=True
            )

        db.execute(
            "INSERT INTO participants (message_id, user_id, joined_at) VALUES (?, ?, ?)",
            (mid, interaction.user.id, int(time.time())),
        )
        db.commit()
        members = get_members(mid)
        r = get_recruit(mid)
        await interaction.response.edit_message(
            embed=build_embed(r, members), view=self
        )

        if len(members) >= r["capacity"] and not r["full_notified"]:
            db.execute(
                "UPDATE recruits SET full_notified = 1 WHERE message_id = ?", (mid,)
            )
            db.commit()
            mentions = " ".join(f"<@{m}>" for m in members)
            await send_to_thread(
                r, f"✅ メンバーが揃いました!\n{mentions}\n開始: <t:{r['start_ts']}:F>"
            )

    @discord.ui.button(
        label="取り消し", style=discord.ButtonStyle.secondary,
        custom_id="recruit:leave",
    )
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        mid = interaction.message.id
        r = get_recruit(mid)
        if r is None or r["status"] != "open":
            return await interaction.response.send_message(
                "この募集は操作できません。", ephemeral=True
            )
        if interaction.user.id == r["owner_id"]:
            return await interaction.response.send_message(
                "募集主は取り消しできません。募集をやめる場合は「募集締切」を押してください。",
                ephemeral=True,
            )
        members = get_members(mid)
        if interaction.user.id not in members:
            return await interaction.response.send_message(
                "参加していません。", ephemeral=True
            )

        db.execute(
            "DELETE FROM participants WHERE message_id = ? AND user_id = ?",
            (mid, interaction.user.id),
        )
        # 空きができたので、再び満員になったら通知できるようにする
        db.execute("UPDATE recruits SET full_notified = 0 WHERE message_id = ?", (mid,))
        db.commit()
        r = get_recruit(mid)
        await interaction.response.edit_message(
            embed=build_embed(r, get_members(mid)), view=self
        )
        await send_to_thread(r, f"{interaction.user.mention} さんが参加を取り消しました。")

    @discord.ui.button(
        label="募集締切", style=discord.ButtonStyle.danger, emoji="🔒",
        custom_id="recruit:close",
    )
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        mid = interaction.message.id
        r = get_recruit(mid)
        if r is None or r["status"] != "open":
            return await interaction.response.send_message(
                "この募集は操作できません。", ephemeral=True
            )
        if interaction.user.id != r["owner_id"] and not is_admin(interaction.user):
            return await interaction.response.send_message(
                "募集主または管理者のみ操作できます。", ephemeral=True
            )
        await interaction.response.defer()
        await finalize(r, "🔒 募集を締め切りました。参加者のみなさん、よろしくお願いします!")


# ---------------------------------------------------------------
# /募集 コマンド:対象ロール選択 → 募集作成
# ---------------------------------------------------------------
class RoleSelectView(discord.ui.View):
    """募集主にだけ見える(ephemeral)対象ロール選択画面"""

    def __init__(self, data: dict):
        super().__init__(timeout=300)
        self.data = data
        self.selected: list[int] = []

    @discord.ui.select(
        placeholder="対象ロールを選択(複数可/未選択なら全員に募集)",
        min_values=0,
        max_values=len(TARGET_ROLES),
        options=[
            discord.SelectOption(label=label, value=str(rid))
            for label, rid in TARGET_ROLES.items()
        ],
    )
    async def pick(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.selected = [int(v) for v in select.values]
        await interaction.response.defer()

    @discord.ui.button(label="この内容で募集する", style=discord.ButtonStyle.success)
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="募集を作成中…", view=None)
        self.stop()
        try:
            await post_recruit(interaction, self.data, self.selected)
        except Exception:
            log.exception("募集の作成に失敗")
            await interaction.edit_original_response(
                content="⚠️ 募集の作成に失敗しました。Botの権限(スレッド作成・メンション権限など)を確認してください。"
            )
            return
        await interaction.edit_original_response(content="✅ 募集を作成しました!")


async def post_recruit(interaction: discord.Interaction, data: dict, role_ids: list[int]):
    channel = interaction.channel
    owner = interaction.user

    # メンション:管理者には必ず通知 + 対象ロール(未指定なら全員)
    mentions = [f"<@&{ADMIN_ROLE_ID}>"]
    if role_ids:
        mentions += [f"<@&{i}>" for i in role_ids]
    else:
        mentions.append("@everyone")
    content = " ".join(dict.fromkeys(mentions))

    now = int(time.time())
    record = {
        "title": data["title"],
        "note": data["note"],
        "start_ts": data["start_ts"],
        "capacity": data["capacity"],
        "roles": json.dumps(role_ids),
        "status": "open",
        "owner_id": owner.id,
    }

    msg = await channel.send(
        content=content,
        embed=build_embed(record, [owner.id]),
        view=RecruitView(),
        allowed_mentions=discord.AllowedMentions(everyone=True, roles=True, users=True),
    )

    start_label = datetime.fromtimestamp(data["start_ts"], JST).strftime("%m/%d %H:%M")
    thread = await msg.create_thread(
        name=f"【募集】{data['title']} {start_label}"[:100],
        auto_archive_duration=1440,
    )

    reminded = 1 if data["start_ts"] - now <= REMIND_BEFORE_SEC else 0
    db.execute(
        """INSERT INTO recruits
           (message_id, channel_id, thread_id, owner_id, title, note, start_ts,
            capacity, roles, status, reminded, full_notified)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, 0)""",
        (
            msg.id, channel.id, thread.id, owner.id, data["title"], data["note"],
            data["start_ts"], data["capacity"], json.dumps(role_ids), reminded,
        ),
    )
    db.execute(
        "INSERT INTO participants (message_id, user_id, joined_at) VALUES (?, ?, ?)",
        (msg.id, owner.id, now),
    )
    db.commit()

    await thread.send(
        f"{owner.mention} 募集スレッドです。参加者との相談はこちらでどうぞ。\n"
        f"開始: <t:{data['start_ts']}:F>"
    )


class RecruitBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.default())

    async def setup_hook(self):
        self.add_view(RecruitView())  # 再起動後もボタンを有効化
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        watcher.start()


bot = RecruitBot()


@bot.tree.command(name="募集", description="パーティ募集を作成します")
@app_commands.rename(title="タイトル", start="開始日時", capacity="定員", note="備考")
@app_commands.describe(
    title="募集内容(例:ゾンビ系TA)",
    start="開始日時(日本時間) 例: 11/15 21:00",
    capacity="募集人数(あなたを含む)",
    note="補足事項(任意)",
)
@app_commands.guild_only()
async def recruit(
    interaction: discord.Interaction,
    title: app_commands.Range[str, 1, 60],
    start: str,
    capacity: app_commands.Range[int, 2, 8],
    note: app_commands.Range[str, 0, 300] | None = None,
):
    if not isinstance(interaction.channel, discord.TextChannel):
        return await interaction.response.send_message(
            "通常のテキストチャンネルで実行してください。", ephemeral=True
        )

    dt = parse_start(start)
    if dt is None:
        return await interaction.response.send_message(
            "❌ 日時の形式が正しくありません。`11/15 21:00` または `2026/11/15 21:00` の形式で入力してください。",
            ephemeral=True,
        )
    if dt <= datetime.now(JST):
        return await interaction.response.send_message(
            "❌ 過去の日時は指定できません。", ephemeral=True
        )

    data = {
        "title": title,
        "note": note,
        "capacity": capacity,
        "start_ts": int(dt.timestamp()),
    }
    await interaction.response.send_message(
        f"**{title}** / <t:{data['start_ts']}:F> / 定員{capacity}人\n"
        "対象ロールを選んで「この内容で募集する」を押してください。\n"
        "(何も選ばなければ全員に募集します)",
        view=RoleSelectView(data),
        ephemeral=True,
    )


# ---------------------------------------------------------------
# 定期チェック:リマインド・自動締切
# ---------------------------------------------------------------
@tasks.loop(minutes=1)
async def watcher():
    now = int(time.time())
    rows = db.execute("SELECT * FROM recruits WHERE status = 'open'").fetchall()
    for r in rows:
        try:
            if now >= r["start_ts"]:
                await finalize(r, "⏰ 開始時刻になりました。募集を終了します。")
            elif not r["reminded"] and now >= r["start_ts"] - REMIND_BEFORE_SEC:
                db.execute(
                    "UPDATE recruits SET reminded = 1 WHERE message_id = ?",
                    (r["message_id"],),
                )
                db.commit()
                mentions = " ".join(f"<@{m}>" for m in get_members(r["message_id"]))
                await send_to_thread(
                    r, f"⏰ まもなく開始です!(<t:{r['start_ts']}:R>)\n{mentions}"
                )
        except Exception:
            log.exception("定期チェックでエラー")


@watcher.before_loop
async def before_watcher():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    log.info("ログインしました: %s", bot.user)


bot.run(TOKEN)
