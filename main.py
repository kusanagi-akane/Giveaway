#made by.kusanagi akane
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Union
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
)
LOG = logging.getLogger("giveaway-bot")

ALLOW_CASE_INSENSITIVE = True
MATCH_MODE = "equals"
TIME_RE = re.compile(
    r"(?:(?P<d>\d+)d)?(?:(?P<h>\d+)h)?(?:(?P<m>\d+)m)?(?:(?P<s>\d+)s)?"
)

TAIWAN_TZ = timezone(timedelta(hours=8))
GIVEAWAY_FILE = Path("giveaways.json")
GIVEAWAY_SYNC_DEBOUNCE_SECONDS = 1.5
GIVEAWAY_SYNC_IDLE_DELAY_SECONDS = 0.35
GIVEAWAY_SYNC_MIN_INTERVAL_SECONDS = 1.5
GIVEAWAY_SYNC_RETRY_DELAY_SECONDS = 5.5
CHANNEL_MESSAGE_EDIT_MIN_INTERVAL_SECONDS = 2.0
STATE_FLUSH_DEBOUNCE_SECONDS = 2.0
MAX_GIVEAWAY_DURATION_SECONDS = 90 * 24 * 60 * 60
STARTUP_SYNC_CONCURRENCY = 3

POSTABLE_CHANNEL_TYPES = [
    discord.ChannelType.text,
    discord.ChannelType.news,
    discord.ChannelType.forum,
    discord.ChannelType.public_thread,
    discord.ChannelType.private_thread,
]

NO_PINGS = discord.AllowedMentions.none()
USER_PINGS = discord.AllowedMentions(
    users=True,
    roles=False,
    everyone=False,
    replied_user=False,
)

PostChannel = Union[discord.TextChannel, discord.Thread, discord.ForumChannel]
RuntimeChannel = Union[discord.TextChannel, discord.Thread]


def now_taiwan() -> datetime:
    return datetime.now(TAIWAN_TZ)


def now_ts() -> float:
    return now_taiwan().timestamp()


def timestamp_short(ts: float, style: str = "F") -> str:
    return f"<t:{int(ts)}:{style}>"


def parse_duration(text: str) -> int:
    text = text.strip().lower()
    if not text:
        raise ValueError("請輸入持續時間，例如 `30m`、`2h`、`1d2h`。")

    if text.isdigit():
        seconds = int(text)
        if seconds <= 0:
            raise ValueError("持續時間必須大於 0 秒。")
        return seconds

    match = TIME_RE.fullmatch(text)
    if not match:
        raise ValueError("時間格式錯誤，請使用 `30m`、`2h`、`1d2h`、`45s`。")

    days = int(match.group("d") or 0)
    hours = int(match.group("h") or 0)
    minutes = int(match.group("m") or 0)
    seconds = int(match.group("s") or 0)
    total = days * 86400 + hours * 3600 + minutes * 60 + seconds
    if total <= 0:
        raise ValueError("持續時間必須大於 0 秒。")
    if total > MAX_GIVEAWAY_DURATION_SECONDS:
        raise ValueError("持續時間不能超過 90 天。")
    return total


def normalize_text(text: str) -> str:
    return text.lower() if ALLOW_CASE_INSENSITIVE else text


def match_phrase(message_content: str, phrase: str) -> bool:
    source = normalize_text(message_content.strip())
    target = normalize_text(phrase.strip())
    if MATCH_MODE == "contains":
        return target in source
    return source == target


def get_role_mentions(role_ids: Set[int], guild: Optional[discord.Guild]) -> str:
    if not role_ids:
        return "未設定"

    mentions: List[str] = []
    for role_id in sorted(role_ids):
        role = guild.get_role(role_id) if guild else None
        mentions.append(role.mention if role else f"<@&{role_id}>")
    return "、".join(mentions)


def get_channel_label(guild: Optional[discord.Guild], channel_id: Optional[int]) -> str:
    if guild and channel_id:
        channel = guild.get_channel(channel_id)
        if channel is None:
            channel = guild.get_thread(channel_id)
        if channel:
            return channel.mention
    if channel_id:
        return f"`{channel_id}`"
    return "目前所在頻道"


def get_member_label(guild: Optional[discord.Guild], user_id: int) -> str:
    if guild:
        member = guild.get_member(user_id)
        if member:
            return member.mention
    return f"<@{user_id}>"


def resolve_post_channel(
    interaction: discord.Interaction,
    selected: Optional[Union[discord.abc.GuildChannel, discord.Thread]],
) -> Optional[PostChannel]:
    channel = selected or interaction.channel
    if isinstance(channel, (discord.TextChannel, discord.Thread, discord.ForumChannel)):
        return channel
    return None


def resolve_runtime_channel(guild: Optional[discord.Guild], channel_id: int) -> Optional[RuntimeChannel]:
    if guild is None:
        return None

    channel = guild.get_channel(channel_id)
    if isinstance(channel, discord.TextChannel):
        return channel

    thread = guild.get_thread(channel_id)
    if isinstance(thread, discord.Thread):
        return thread

    return None


def int_or_zero(raw: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def parse_nonnegative_int_field(
    raw: str,
    *,
    field_name: str,
    default: Optional[int] = None,
    minimum: int = 0,
    maximum: Optional[int] = None,
) -> int:
    text = raw.strip()
    if not text:
        if default is None:
            raise ValueError(f"{field_name}不能留空。")
        value = default
    else:
        if not text.isdigit():
            raise ValueError(f"{field_name}必須是整數。")
        value = int(text)

    if value < minimum:
        raise ValueError(f"{field_name}不能小於 {minimum}。")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field_name}不能大於 {maximum}。")
    return value


def normalize_image_url(raw: str) -> Optional[str]:
    text = raw.strip()
    if not text:
        return None

    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("附圖請填可公開存取的 `http://` 或 `https://` 圖片網址。")
    return text


def summarize_image_url(image_url: Optional[str]) -> str:
    if not image_url:
        return "未設定"
    if len(image_url) <= 60:
        return image_url
    return image_url[:57] + "..."


def summarize_custom_message(custom_message: Optional[str]) -> str:
    if not custom_message:
        return "未設定"

    flattened = " ".join(part.strip() for part in custom_message.splitlines() if part.strip())
    if not flattened:
        return "未設定"
    if len(flattened) <= 60:
        return flattened
    return flattened[:57] + "..."


def parse_guild_id_set(raw: str, *, current_guild_id: Optional[int] = None) -> Set[int]:
    text = raw.strip()
    if not text:
        return set()

    values = re.split(r"[\s,，]+", text)
    guild_ids: Set[int] = set()
    invalid: List[str] = []

    for value in values:
        if not value:
            continue
        if not value.isdigit():
            invalid.append(value)
            continue
        guild_id = int(value)
        if current_guild_id is not None and guild_id == current_guild_id:
            continue
        guild_ids.add(guild_id)

    if invalid:
        raise ValueError("群組 ID 格式錯誤，請使用純數字，並以換行、空白或逗號分隔。")

    return guild_ids


def format_guild_labels(
    guild_ids: Set[int],
    bot: Optional[discord.Client] = None,
) -> str:
    if not guild_ids:
        return "未設定"

    parts: List[str] = []
    for guild_id in sorted(guild_ids):
        guild = bot.get_guild(guild_id) if bot else None
        if guild is not None:
            parts.append(f"{guild.name} (`{guild_id}`)")
        else:
            parts.append(f"`{guild_id}`")
    return "、".join(parts)


def add_optional_media_gallery(
    container: discord.ui.Container,
    image_url: Optional[str],
    *,
    description: Optional[str] = None,
) -> None:
    if not image_url:
        return

    gallery = discord.ui.MediaGallery()
    gallery.add_item(media=image_url, description=(description or None))
    container.add_item(gallery)


def write_json_atomic(path: Path, payload: dict) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(path)


@dataclass(slots=True)
class Giveaway:
    guild_id: int
    channel_id: int
    message_id: int
    prize: str
    winners: int
    host_id: int
    starts_at_unix: float
    ends_at_unix: float
    image_url: Optional[str] = None
    custom_message: Optional[str] = None
    required_guild_ids: Set[int] = field(default_factory=set)
    must_said: Optional[str] = None
    winner_ids: List[int] = field(default_factory=list)
    said_users: Set[int] = field(default_factory=set)
    reacted_users: Set[int] = field(default_factory=set)
    ended: bool = False
    required_role_ids: Set[int] = field(default_factory=set)
    excluded_role_ids: Set[int] = field(default_factory=set)
    min_join_days: int = 0
    min_messages: int = 0
    msg_counts: Dict[int, int] = field(default_factory=dict)


@dataclass(slots=True)
class GiveawayDraft:
    prize: str = ""
    duration_text: str = ""
    duration_seconds: int = 0
    winners: int = 1
    image_url: str = ""
    custom_message: str = ""
    required_guild_ids: Set[int] = field(default_factory=set)
    target_channel_id: Optional[int] = None
    must_said: str = ""
    required_role_ids: Set[int] = field(default_factory=set)
    excluded_role_ids: Set[int] = field(default_factory=set)
    min_join_days: int = 0
    min_messages: int = 0

    @property
    def ready(self) -> bool:
        return bool(self.prize and self.duration_seconds > 0 and self.winners > 0)


def giveaway_to_dict(giveaway: Giveaway) -> dict:
    data = asdict(giveaway)
    data["said_users"] = list(giveaway.said_users)
    data["reacted_users"] = list(giveaway.reacted_users)
    data["required_guild_ids"] = list(giveaway.required_guild_ids)
    data["required_role_ids"] = list(giveaway.required_role_ids)
    data["excluded_role_ids"] = list(giveaway.excluded_role_ids)
    data["msg_counts"] = {str(user_id): count for user_id, count in giveaway.msg_counts.items()}
    return data


def giveaway_from_dict(data: dict) -> Giveaway:
    return Giveaway(
        guild_id=int(data["guild_id"]),
        channel_id=int(data["channel_id"]),
        message_id=int(data["message_id"]),
        prize=str(data["prize"]),
        winners=int(data["winners"]),
        host_id=int(data["host_id"]),
        starts_at_unix=float(data.get("starts_at_unix", now_ts())),
        ends_at_unix=float(data["ends_at_unix"]),
        image_url=data.get("image_url"),
        custom_message=data.get("custom_message"),
        required_guild_ids=set(int(v) for v in data.get("required_guild_ids", [])),
        must_said=data.get("must_said"),
        winner_ids=[int(v) for v in data.get("winner_ids", [])],
        said_users=set(int(v) for v in data.get("said_users", [])),
        reacted_users=set(int(v) for v in data.get("reacted_users", [])),
        ended=bool(data.get("ended", False)),
        required_role_ids=set(int(v) for v in data.get("required_role_ids", [])),
        excluded_role_ids=set(int(v) for v in data.get("excluded_role_ids", [])),
        min_join_days=int(data.get("min_join_days", 0) or 0),
        min_messages=int(data.get("min_messages", 0) or 0),
        msg_counts={int(k): int(v) for k, v in data.get("msg_counts", {}).items()},
    )


def get_requirement_failure_reasons(
    member: discord.Member,
    giveaway: Giveaway,
    *,
    bot: Optional[discord.Client] = None,
) -> List[str]:
    reasons: List[str] = []
    role_ids = {role.id for role in member.roles}

    if giveaway.required_role_ids and role_ids.isdisjoint(giveaway.required_role_ids):
        reasons.append("缺少必要身分組。")
    if giveaway.excluded_role_ids and not role_ids.isdisjoint(giveaway.excluded_role_ids):
        reasons.append("持有排除身分組。")
    if giveaway.required_guild_ids:
        if bot is None:
            reasons.append("目前無法驗證跨群資格。")
        else:
            for guild_id in giveaway.required_guild_ids:
                external_guild = bot.get_guild(guild_id)
                if external_guild is None or external_guild.get_member(member.id) is None:
                    reasons.append(f"尚未加入必要群組 `{guild_id}`。")
    if giveaway.min_join_days > 0:
        if member.joined_at is None:
            reasons.append("目前無法驗證伺服器加入天數。")
        elif (datetime.now(timezone.utc) - member.joined_at).days < giveaway.min_join_days:
            reasons.append(f"加入伺服器未滿 {giveaway.min_join_days} 天。")
    if giveaway.must_said and member.id not in giveaway.said_users:
        reasons.append("尚未說出指定訊息。")
    if giveaway.min_messages > 0 and giveaway.msg_counts.get(member.id, 0) < giveaway.min_messages:
        reasons.append(f"抽獎期間發言數未達 {giveaway.min_messages} 則。")

    return reasons


def member_meets_requirements(
    member: discord.Member,
    giveaway: Giveaway,
    *,
    bot: Optional[discord.Client] = None,
) -> bool:
    return not get_requirement_failure_reasons(member, giveaway, bot=bot)


def eligible_user_ids(
    guild: Optional[discord.Guild],
    giveaway: Giveaway,
    *,
    bot: Optional[discord.Client] = None,
) -> Set[int]:
    if guild is None:
        return set()

    eligible: Set[int] = set()
    for user_id in giveaway.reacted_users:
        member = guild.get_member(user_id)
        if member is None or member.bot:
            continue
        if member_meets_requirements(member, giveaway, bot=bot):
            eligible.add(user_id)
    return eligible


def giveaway_condition_lines(
    giveaway: Giveaway,
    guild: Optional[discord.Guild],
    *,
    bot: Optional[discord.Client] = None,
    include_join_hint: bool = True,
) -> List[str]:
    lines: List[str] = []

    if giveaway.must_said:
        lines.append(f"- 必須在抽獎期間傳送完全符合的訊息：`{giveaway.must_said}`")
    if giveaway.required_guild_ids:
        lines.append(
            f"- 必須同時加入這些群組：{format_guild_labels(giveaway.required_guild_ids, bot)}"
        )
    if giveaway.required_role_ids:
        lines.append(f"- 需要擁有身分組：{get_role_mentions(giveaway.required_role_ids, guild)}")
    if giveaway.excluded_role_ids:
        lines.append(f"- 不能擁有身分組：{get_role_mentions(giveaway.excluded_role_ids, guild)}")
    if giveaway.min_join_days > 0:
        lines.append(f"- 加入伺服器至少 {giveaway.min_join_days} 天")
    if giveaway.min_messages > 0:
        lines.append(f"- 抽獎期間至少發送 {giveaway.min_messages} 則訊息")
    if include_join_hint:
        lines.append("- 參加方式：按下下方按鈕")

    return lines or ["- 無額外門檻"]


def draft_basic_lines(draft: GiveawayDraft) -> List[str]:
    return [
        f"- 獎品：{draft.prize or '尚未設定'}",
        f"- 持續時間：{draft.duration_text or '尚未設定'}",
        f"- 得獎名額：{draft.winners}",
        f"- 附圖：{summarize_image_url(draft.image_url or None)}",
        f"- 自訂訊息：{summarize_custom_message(draft.custom_message or None)}",
    ]


def draft_condition_lines(
    draft: GiveawayDraft,
    guild: Optional[discord.Guild],
    *,
    bot: Optional[discord.Client] = None,
) -> List[str]:
    temp = Giveaway(
        guild_id=guild.id if guild else 0,
        channel_id=0,
        message_id=0,
        prize=draft.prize or "預覽獎品",
        winners=max(1, draft.winners),
        host_id=0,
        starts_at_unix=now_ts(),
        ends_at_unix=now_ts() + max(draft.duration_seconds, 60),
        custom_message=draft.custom_message or None,
        required_guild_ids=set(draft.required_guild_ids),
        must_said=draft.must_said or None,
        required_role_ids=set(draft.required_role_ids),
        excluded_role_ids=set(draft.excluded_role_ids),
        min_join_days=max(0, draft.min_join_days),
        min_messages=max(0, draft.min_messages),
    )
    return giveaway_condition_lines(temp, guild, bot=bot, include_join_hint=True)


def giveaway_overview_text(guild: Optional[discord.Guild], giveaway: Giveaway) -> str:
    return "\n".join(
        [
        f"### {giveaway.prize}",
        f"- 主辦人：{get_member_label(guild, giveaway.host_id)}",
        f"- 得獎名額：{giveaway.winners}",
        f"- 結束時間：{timestamp_short(giveaway.ends_at_unix)} ({timestamp_short(giveaway.ends_at_unix, 'R')})",
        ]
    )


def giveaway_status_text(
    guild: Optional[discord.Guild],
    giveaway: Giveaway,
    *,
    bot: Optional[discord.Client] = None,
    winner_ids: Optional[Sequence[int]] = None,
    empty_reason: Optional[str] = None,
) -> str:
    total = len(giveaway.reacted_users)
    eligible_count = len(eligible_user_ids(guild, giveaway, bot=bot))
    lines = [
        "### 目前狀態",
        f"- 已參加：**{total}** 人",
        f"- 符合資格：**{eligible_count}** 人",
    ]

    if winner_ids is not None:
        if winner_ids:
            winner_mentions = " ".join(get_member_label(guild, user_id) for user_id in winner_ids)
            lines.append(f"- 得獎者：{winner_mentions}")
        elif empty_reason:
            lines.append(f"- 結果：{empty_reason}")

    return "\n".join(lines)


def giveaway_active_render_signature(
    guild: Optional[discord.Guild],
    giveaway: Giveaway,
    *,
    bot: Optional[discord.Client] = None,
) -> tuple[int, int]:
    return (
        len(giveaway.reacted_users),
        len(eligible_user_ids(guild, giveaway, bot=bot)),
    )


class PanelLayout(discord.ui.LayoutView):
    def __init__(
        self,
        title: str,
        body: str,
        *,
        accent_color: Optional[discord.Colour] = None,
        image_url: Optional[str] = None,
        timeout: Optional[float] = 120,
    ) -> None:
        super().__init__(timeout=timeout)
        container = discord.ui.Container(accent_color=accent_color)
        container.add_item(discord.ui.TextDisplay(f"## {title}"))
        container.add_item(discord.ui.TextDisplay(body))
        add_optional_media_gallery(container, image_url, description=title)
        self.add_item(container)


class ParticipantsLayout(discord.ui.LayoutView):
    def __init__(
        self,
        bot: "GiveawayBot",
        message_id: int,
        owner_id: int,
        *,
        eligible_ids: Optional[Set[int]] = None,
        page: int = 0,
        per_page: int = 20,
    ) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.message_id = message_id
        self.owner_id = owner_id
        self.eligible_ids = set(eligible_ids) if eligible_ids is not None else None
        self.page = page
        self.per_page = per_page
        self._rebuild()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True

        await interaction.response.send_message(
            view=PanelLayout(
                "不能操作",
                "這個名單視窗只屬於發起查看的人。",
                accent_color=discord.Colour.red(),
            ),
            ephemeral=True,
        )
        return False

    def _rebuild(self) -> None:
        self.clear_items()
        giveaway = self.bot.giveaways.get(self.message_id)
        guild = self.bot.get_guild(giveaway.guild_id) if giveaway else None
        ids = sorted(giveaway.reacted_users) if giveaway else []
        if self.eligible_ids is not None:
            eligible_count = len(self.eligible_ids)
        else:
            eligible_count = len(eligible_user_ids(guild, giveaway, bot=self.bot)) if giveaway else 0

        start = self.page * self.per_page
        end = start + self.per_page
        chunk = ids[start:end]

        lines: List[str] = []
        for index, user_id in enumerate(chunk, start=start + 1):
            if self.eligible_ids is None:
                lines.append(f"{index}. {get_member_label(guild, user_id)}")
                continue

            status = "符合資格" if user_id in self.eligible_ids else "未符合資格"
            lines.append(f"{index}. {get_member_label(guild, user_id)} ({status})")

        page_count = max(1, (len(ids) + self.per_page - 1) // self.per_page)
        body = "\n".join(lines) if lines else "- 目前還沒有人加入。"

        container = discord.ui.Container(accent_color=discord.Colour.blurple())
        container.add_item(discord.ui.TextDisplay("## 參加名單"))
        container.add_item(discord.ui.TextDisplay(body))

        row = discord.ui.ActionRow()
        prev_button = discord.ui.Button(
            label="上一頁",
            style=discord.ButtonStyle.secondary,
            disabled=self.page <= 0,
        )
        next_button = discord.ui.Button(
            label="下一頁",
            style=discord.ButtonStyle.secondary,
            disabled=self.page >= page_count - 1,
        )
        prev_button.callback = self.prev_page
        next_button.callback = self.next_page
        row.add_item(prev_button)
        row.add_item(next_button)
        container.add_item(row)
        container.add_item(
            discord.ui.TextDisplay(
                "\n".join(
                    [
                        f"- 第 **{self.page + 1}** / **{page_count}** 頁",
                        f"- 已參加：**{len(ids)}** 人",
                        f"- 目前符合資格：**{eligible_count}** 人",
                        "- 這裡顯示的是參加名單，不代表全部都符合資格。",
                    ]
                )
            )
        )
        self.add_item(container)

    async def prev_page(self, interaction: discord.Interaction) -> None:
        if self.page > 0:
            self.page -= 1
        self._rebuild()
        await interaction.response.edit_message(view=self)

    async def next_page(self, interaction: discord.Interaction) -> None:
        giveaway = self.bot.giveaways.get(self.message_id)
        total = len(giveaway.reacted_users) if giveaway else 0
        max_page = max(0, (total - 1) // self.per_page)
        if self.page < max_page:
            self.page += 1
        self._rebuild()
        await interaction.response.edit_message(view=self)


class LeaveGiveawayLayout(discord.ui.LayoutView):
    def __init__(self, bot: "GiveawayBot", message_id: int) -> None:
        super().__init__(timeout=90)
        self.bot = bot
        self.message_id = message_id

        leave_button = discord.ui.Button(
            label="離開抽獎",
            style=discord.ButtonStyle.danger,
        )
        leave_button.callback = self.leave_callback

        container = discord.ui.Container(accent_color=discord.Colour.orange())
        container.add_item(discord.ui.TextDisplay("## 你已加入抽獎"))
        container.add_item(
            discord.ui.Section(
                discord.ui.TextDisplay(
                    "- 如果要退出，直接按右側按鈕。\n- 離開後會立即更新抽獎卡片上的人數。"
                ),
                accessory=leave_button,
            )
        )
        self.add_item(container)

    async def leave_callback(self, interaction: discord.Interaction) -> None:
        giveaway = self.bot.giveaways.get(self.message_id)
        if giveaway is None or giveaway.ended:
            await interaction.response.send_message(
                view=PanelLayout(
                    "抽獎不可用",
                    "這個抽獎不存在，或是已經結束。",
                    accent_color=discord.Colour.red(),
                ),
                ephemeral=True,
            )
            return

        if interaction.user.id not in giveaway.reacted_users:
            await interaction.response.send_message(
                view=PanelLayout(
                    "你不在名單內",
                    "目前沒有需要退出的參加紀錄。",
                    accent_color=discord.Colour.orange(),
                ),
                ephemeral=True,
            )
            return

        giveaway.reacted_users.discard(interaction.user.id)
        self.bot.save_giveaways()
        self.bot.schedule_giveaway_message_sync(self.message_id)
        await interaction.response.send_message(
            view=PanelLayout(
                "已離開抽獎",
                "你的參加紀錄已移除。",
                accent_color=discord.Colour.orange(),
            ),
            ephemeral=True,
        )


class ActiveGiveawayLayout(discord.ui.LayoutView):
    def __init__(self, bot: "GiveawayBot", message_id: int) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.message_id = message_id
        self._rebuild()

    def _rebuild(self) -> None:
        self.clear_items()
        giveaway = self.bot.giveaways.get(self.message_id)

        if giveaway is None:
            self.add_item(
                discord.ui.Container(
                    discord.ui.TextDisplay("## 抽獎不存在"),
                    discord.ui.TextDisplay("- 這個抽獎可能已經結束或被移除。"),
                    accent_color=discord.Colour.red(),
                )
            )
            return

        guild = self.bot.get_guild(giveaway.guild_id)
        join_button = discord.ui.Button(
            label="加入抽獎",
            style=discord.ButtonStyle.success,
            custom_id=f"giveaway:join:{self.message_id}",
        )
        join_button.callback = self.join_callback

        members_button = discord.ui.Button(
            label="查看名單",
            style=discord.ButtonStyle.secondary,
            custom_id=f"giveaway:members:{self.message_id}",
        )
        members_button.callback = self.members_callback

        container = discord.ui.Container(accent_color=discord.Colour.green())
        container.add_item(discord.ui.TextDisplay("## 抽獎進行中"))
        container.add_item(discord.ui.TextDisplay(giveaway_overview_text(guild, giveaway)))
        add_optional_media_gallery(
            container,
            giveaway.image_url,
            description=f"{giveaway.prize} 的附圖",
        )
        if giveaway.custom_message:
            container.add_item(discord.ui.TextDisplay(giveaway.custom_message))
        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.Section(
                discord.ui.TextDisplay(giveaway_status_text(guild, giveaway, bot=self.bot)),
                accessory=join_button,
            )
        )
        container.add_item(
            discord.ui.TextDisplay(
                "### 參加條件\n"
                + "\n".join(giveaway_condition_lines(giveaway, guild, bot=self.bot))
            )
        )

        row = discord.ui.ActionRow()
        row.add_item(members_button)
        container.add_item(row)
        self.add_item(container)

    async def join_callback(self, interaction: discord.Interaction) -> None:
        giveaway = self.bot.giveaways.get(self.message_id)
        if giveaway is None:
            await interaction.response.send_message(
                view=PanelLayout(
                    "抽獎不存在",
                    "這個抽獎找不到，可能已經結束。",
                    accent_color=discord.Colour.red(),
                ),
                ephemeral=True,
            )
            return

        if giveaway.ended:
            await interaction.response.send_message(
                view=PanelLayout(
                    "抽獎已結束",
                    "這個抽獎不能再加入了。",
                    accent_color=discord.Colour.red(),
                ),
                ephemeral=True,
            )
            return

        if interaction.user.id in giveaway.reacted_users:
            await interaction.response.send_message(
                view=LeaveGiveawayLayout(self.bot, self.message_id),
                ephemeral=True,
            )
            return

        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                view=PanelLayout(
                    "無法加入抽獎",
                    "目前無法驗證你的伺服器成員資料，請稍後再試。",
                    accent_color=discord.Colour.red(),
                ),
                ephemeral=True,
            )
            return

        reasons = await self.bot.get_join_failure_reasons(interaction.user, giveaway)
        if reasons:
            await interaction.response.send_message(
                view=PanelLayout(
                    "目前不符合抽獎資格",
                    "\n".join(f"- {reason}" for reason in reasons),
                    accent_color=discord.Colour.red(),
                ),
                ephemeral=True,
            )
            return

        giveaway.reacted_users.add(interaction.user.id)
        self.bot.save_giveaways()
        self.bot.schedule_giveaway_message_sync(self.message_id)
        await interaction.response.send_message(
            view=LeaveGiveawayLayout(self.bot, self.message_id),
            ephemeral=True,
        )

    async def members_callback(self, interaction: discord.Interaction) -> None:
        giveaway = self.bot.giveaways.get(self.message_id)
        if giveaway is None:
            await interaction.response.send_message(
                view=PanelLayout(
                    "抽獎不存在",
                    "這個抽獎找不到，可能已經結束。",
                    accent_color=discord.Colour.red(),
                ),
                ephemeral=True,
            )
            return

        guild = self.bot.get_guild(giveaway.guild_id)
        await interaction.response.defer(ephemeral=True, thinking=True)
        eligible_ids = await self.bot.collect_eligible_user_ids(guild, giveaway)
        await interaction.followup.send(
            view=ParticipantsLayout(
                self.bot,
                self.message_id,
                interaction.user.id,
                eligible_ids=eligible_ids,
            ),
            ephemeral=True,
        )


class EndedGiveawayLayout(discord.ui.LayoutView):
    def __init__(
        self,
        guild: Optional[discord.Guild],
        giveaway: Giveaway,
        *,
        bot: Optional[discord.Client] = None,
        winner_ids: Sequence[int],
        empty_reason: Optional[str] = None,
    ) -> None:
        super().__init__(timeout=300)
        title = "## 抽獎已結束"
        if empty_reason:
            title = "## 抽獎已結束，無人符合資格"

        container = discord.ui.Container(accent_color=discord.Colour.red())
        container.add_item(discord.ui.TextDisplay(title))
        container.add_item(discord.ui.TextDisplay(giveaway_overview_text(guild, giveaway)))
        add_optional_media_gallery(
            container,
            giveaway.image_url,
            description=f"{giveaway.prize} 的附圖",
        )
        if giveaway.custom_message:
            container.add_item(discord.ui.TextDisplay(giveaway.custom_message))
        container.add_item(discord.ui.Separator())
        container.add_item(
            discord.ui.TextDisplay(
                giveaway_status_text(
                    guild,
                    giveaway,
                    bot=bot,
                    winner_ids=winner_ids,
                    empty_reason=empty_reason,
                )
            )
        )
        container.add_item(
            discord.ui.TextDisplay(
                "### 參加條件\n"
                + "\n".join(
                    giveaway_condition_lines(
                        giveaway,
                        guild,
                        bot=bot,
                        include_join_hint=False,
                    )
                )
            )
        )
        self.add_item(container)


class BasicSettingsModal(discord.ui.Modal, title="抽獎基本設定"):
    prize = discord.ui.TextInput(
        label="獎品",
        placeholder="例如：Nitro、序號、週邊",
        max_length=100,
        required=True,
    )
    duration = discord.ui.TextInput(
        label="持續時間",
        placeholder="例如：30m、2h、1d2h",
        max_length=20,
        required=True,
    )
    winners = discord.ui.TextInput(
        label="得獎人數",
        placeholder="預設 1",
        max_length=3,
        required=False,
    )
    image_url = discord.ui.TextInput(
        label="附圖網址",
        placeholder="可留空；請填公開的 https:// 圖片網址",
        max_length=400,
        required=False,
    )
    custom_message = discord.ui.TextInput(
        label="自訂訊息",
        placeholder="可留空；支援多行補充說明、規則、提醒",
        style=discord.TextStyle.paragraph,
        max_length=1000,
        required=False,
    )

    def __init__(self, parent_view: "GiveawaySetupView") -> None:
        super().__init__()
        self.parent_view = parent_view
        draft = parent_view.draft

        if draft.prize:
            self.prize.default = draft.prize
        if draft.duration_text:
            self.duration.default = draft.duration_text
        if draft.winners:
            self.winners.default = str(draft.winners)
        if draft.image_url:
            self.image_url.default = draft.image_url
        if draft.custom_message:
            self.custom_message.default = draft.custom_message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        prize = str(self.prize.value).strip()
        duration_text = str(self.duration.value).strip()
        winners_text = str(self.winners.value).strip()
        image_url_text = str(self.image_url.value).strip()
        custom_message_text = str(self.custom_message.value).strip()

        try:
            duration_seconds = parse_duration(duration_text)
        except ValueError as exc:
            await interaction.response.send_message(
                view=PanelLayout(
                    "時間格式錯誤",
                    str(exc),
                    accent_color=discord.Colour.red(),
                ),
                ephemeral=True,
            )
            return

        try:
            image_url = normalize_image_url(image_url_text)
        except ValueError as exc:
            await interaction.response.send_message(
                view=PanelLayout(
                    "附圖網址錯誤",
                    str(exc),
                    accent_color=discord.Colour.red(),
                ),
                ephemeral=True,
            )
            return

        try:
            winners = parse_nonnegative_int_field(
                winners_text,
                field_name="得獎人數",
                default=1,
                minimum=1,
                maximum=50,
            )
        except ValueError as exc:
            await interaction.response.send_message(
                view=PanelLayout(
                    "得獎人數錯誤",
                    str(exc),
                    accent_color=discord.Colour.red(),
                ),
                ephemeral=True,
            )
            return

        self.parent_view.draft.prize = prize
        self.parent_view.draft.duration_text = duration_text
        self.parent_view.draft.duration_seconds = duration_seconds
        self.parent_view.draft.winners = winners
        self.parent_view.draft.image_url = image_url or ""
        self.parent_view.draft.custom_message = custom_message_text
        self.parent_view._rebuild()

        await interaction.response.send_message(
            view=PanelLayout(
                "基本設定已更新",
                "- 已套用新的獎品、持續時間與名額。\n- 原設定面板也會同步刷新。",
                accent_color=discord.Colour.green(),
            ),
            ephemeral=True,
        )
        await self.parent_view.refresh_message()


class RequirementSettingsModal(discord.ui.Modal, title="抽獎資格設定"):
    must_said = discord.ui.TextInput(
        label="必須說過的訊息",
        placeholder="可留空；需要完全符合",
        required=False,
        max_length=60,
    )
    min_join_days = discord.ui.TextInput(
        label="加入伺服器至少幾天",
        placeholder="可留空；預設 0",
        required=False,
        max_length=4,
    )
    min_messages = discord.ui.TextInput(
        label="抽獎期間至少幾則訊息",
        placeholder="可留空；預設 0",
        required=False,
        max_length=6,
    )

    def __init__(self, parent_view: "GiveawaySetupView") -> None:
        super().__init__()
        self.parent_view = parent_view
        draft = parent_view.draft

        if draft.must_said:
            self.must_said.default = draft.must_said
        if draft.min_join_days:
            self.min_join_days.default = str(draft.min_join_days)
        if draft.min_messages:
            self.min_messages.default = str(draft.min_messages)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        must_said = str(self.must_said.value).strip()
        join_days_text = str(self.min_join_days.value).strip()
        min_messages_text = str(self.min_messages.value).strip()

        try:
            min_join_days = parse_nonnegative_int_field(
                join_days_text,
                field_name="加入天數",
                default=0,
            )
            min_messages = parse_nonnegative_int_field(
                min_messages_text,
                field_name="最少訊息數",
                default=0,
            )
        except ValueError as exc:
            await interaction.response.send_message(
                view=PanelLayout(
                    "資格設定錯誤",
                    str(exc),
                    accent_color=discord.Colour.red(),
                ),
                ephemeral=True,
            )
            return

        self.parent_view.draft.must_said = must_said
        self.parent_view.draft.min_join_days = min_join_days
        self.parent_view.draft.min_messages = min_messages
        self.parent_view._rebuild()

        await interaction.response.send_message(
            view=PanelLayout(
                "資格設定已更新",
                "- 已套用新的訊息、加入天數與發言門檻。\n- 身分組條件仍可直接在原面板調整。",
                accent_color=discord.Colour.green(),
            ),
            ephemeral=True,
        )
        await self.parent_view.refresh_message()


class CrossGuildSettingsModal(discord.ui.Modal, title="跨群加入資格"):
    required_guilds = discord.ui.TextInput(
        label="必須加入的群組 ID",
        placeholder="一行一個 ID，也支援空白或逗號分隔",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=1000,
    )

    def __init__(self, parent_view: "GiveawaySetupView") -> None:
        super().__init__()
        self.parent_view = parent_view
        if parent_view.draft.required_guild_ids:
            self.required_guilds.default = "\n".join(
                str(guild_id) for guild_id in sorted(parent_view.draft.required_guild_ids)
            )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            guild_ids = parse_guild_id_set(
                str(self.required_guilds.value),
                current_guild_id=self.parent_view.guild.id,
            )
        except ValueError as exc:
            await interaction.response.send_message(
                view=PanelLayout(
                    "跨群設定錯誤",
                    str(exc),
                    accent_color=discord.Colour.red(),
                ),
                ephemeral=True,
            )
            return

        unavailable = [
            guild_id
            for guild_id in sorted(guild_ids)
            if self.parent_view.bot.get_guild(guild_id) is None
        ]
        if unavailable:
            await interaction.response.send_message(
                view=PanelLayout(
                    "有些群組無法檢查",
                    "\n".join(
                        [
                            "- 下列群組 ID 不在機器人的快取中："
                            + "、".join(f"`{guild_id}`" for guild_id in unavailable),
                            "- 需要把機器人也邀進那些群組後，這個條件才可用。",
                        ]
                    ),
                    accent_color=discord.Colour.red(),
                ),
                ephemeral=True,
            )
            return

        self.parent_view.draft.required_guild_ids = guild_ids
        self.parent_view._rebuild()
        await interaction.response.send_message(
            view=PanelLayout(
                "跨群資格已更新",
                "\n".join(
                    [
                        f"- 目前要求：{format_guild_labels(guild_ids, self.parent_view.bot)}",
                        "- 參加者必須同時在所有指定群組內。",
                    ]
                ),
                accent_color=discord.Colour.green(),
            ),
            ephemeral=True,
        )
        await self.parent_view.refresh_message()


class GiveawaySetupView(discord.ui.LayoutView):
    def __init__(
        self,
        bot: "GiveawayBot",
        guild: discord.Guild,
        author_id: int,
    ) -> None:
        super().__init__(timeout=900)
        self.bot = bot
        self.guild = guild
        self.author_id = author_id
        self.draft = GiveawayDraft()
        self.message: Optional[discord.InteractionMessage] = None
        self._rebuild()

    def bind_message(self, message: discord.InteractionMessage) -> None:
        self.message = message

    async def refresh_message(self) -> None:
        if self.message is not None:
            await self.message.edit(view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id:
            return True

        await interaction.response.send_message(
            view=PanelLayout(
                "不能操作",
                "這個設定面板只屬於建立抽獎的人。",
                accent_color=discord.Colour.red(),
            ),
            ephemeral=True,
        )
        return False

    def _readiness_text(self) -> str:
        missing: List[str] = []
        if not self.draft.prize:
            missing.append("獎品")
        if self.draft.duration_seconds <= 0:
            missing.append("持續時間")

        if not missing:
            return "- 目前設定完整，可以直接建立抽獎。"
        return "- 尚未完成的欄位：" + "、".join(missing)

    def _cross_guild_summary_lines(self) -> List[str]:
        selected = format_guild_labels(self.draft.required_guild_ids, self.bot)
        lines = [f"- 目前要求：{selected}"]
        lines.append("- 使用方式：一行一個群組 ID，也支援空白或逗號分隔。")
        lines.append("- 只有機器人也在那些群組裡，這個條件才會生效。")
        return lines

    def _custom_message_summary_lines(self) -> List[str]:
        lines = [f"- 目前內容：{summarize_custom_message(self.draft.custom_message or None)}"]
        lines.append("- 這段文字會顯示在抽獎卡與結束結果。")
        lines.append("- 適合放活動補充、領獎方式、注意事項。")
        return lines

    def _preview_text(self) -> str:
        draft = self.draft
        end_ts = now_ts() + max(draft.duration_seconds, 60)
        lines = [
            f"### {draft.prize or '尚未設定獎品'}",
            f"- 發布位置：{get_channel_label(self.guild, draft.target_channel_id)}",
            f"- 得獎名額：{max(1, draft.winners)}",
            f"- 結束時間：{timestamp_short(end_ts)} ({timestamp_short(end_ts, 'R')})",
            f"- 附圖：{summarize_image_url(draft.image_url or None)}",
            "",
            "### 預計條件",
            *draft_condition_lines(draft, self.guild, bot=self.bot),
        ]
        if draft.custom_message:
            lines.extend(["", "### 自訂訊息預覽", draft.custom_message])
        return "\n".join(lines)

    def _rebuild(self) -> None:
        self.clear_items()

        container = discord.ui.Container(accent_color=discord.Colour.blurple())
        container.add_item(discord.ui.TextDisplay("## 建立抽獎"))
        container.add_item(
            discord.ui.TextDisplay(
                "- 先設定基本資料，再補資格條件。\n- 頻道與身分組可直接在這個面板調整。"
            )
        )
        container.add_item(discord.ui.Separator())

        basic_button = discord.ui.Button(
            label="基本設定",
            style=discord.ButtonStyle.primary,
        )
        basic_button.callback = self.open_basic_modal
        container.add_item(
            discord.ui.Section(
                discord.ui.TextDisplay("### 基本資料\n" + "\n".join(draft_basic_lines(self.draft))),
                accessory=basic_button,
            )
        )

        requirement_button = discord.ui.Button(
            label="資格設定",
            style=discord.ButtonStyle.primary,
        )
        requirement_button.callback = self.open_requirements_modal
        container.add_item(
            discord.ui.Section(
                discord.ui.TextDisplay(
                    "### 資格條件\n"
                    + "\n".join(draft_condition_lines(self.draft, self.guild, bot=self.bot))
                ),
                accessory=requirement_button,
            )
        )

        container.add_item(
            discord.ui.TextDisplay(
                "### 補充說明\n"
                + "\n".join(self._custom_message_summary_lines())
                + "\n- 編輯方式：按上方「基本設定」。"
            )
        )

        cross_guild_button = discord.ui.Button(
            label="跨群資格",
            style=discord.ButtonStyle.primary,
        )
        cross_guild_button.callback = self.open_cross_guild_modal
        container.add_item(
            discord.ui.Section(
                discord.ui.TextDisplay("### 跨群加入條件\n" + "\n".join(self._cross_guild_summary_lines())),
                accessory=cross_guild_button,
            )
        )

        target_button = discord.ui.Button(
            label="使用目前頻道",
            style=discord.ButtonStyle.secondary,
        )
        target_button.callback = self.use_current_channel
        container.add_item(
            discord.ui.Section(
                discord.ui.TextDisplay(
                    "### 發布位置\n"
                    + f"- 目前設定：{get_channel_label(self.guild, self.draft.target_channel_id)}"
                ),
                accessory=target_button,
            )
        )

        channel_row = discord.ui.ActionRow()
        channel_select = discord.ui.ChannelSelect(
            channel_types=POSTABLE_CHANNEL_TYPES,
            min_values=0,
            max_values=1,
            placeholder="選擇發布頻道、討論串或 Forum",
        )
        channel_select.callback = self.select_channel
        channel_row.add_item(channel_select)
        container.add_item(channel_row)

        required_row = discord.ui.ActionRow()
        required_select = discord.ui.RoleSelect(
            min_values=0,
            max_values=25,
            placeholder="選擇需要擁有的身分組",
        )
        required_select.callback = self.select_required_roles
        required_row.add_item(required_select)
        container.add_item(required_row)

        excluded_row = discord.ui.ActionRow()
        excluded_select = discord.ui.RoleSelect(
            min_values=0,
            max_values=25,
            placeholder="選擇不能擁有的身分組",
        )
        excluded_select.callback = self.select_excluded_roles
        excluded_row.add_item(excluded_select)
        container.add_item(excluded_row)

        clear_roles_row = discord.ui.ActionRow()
        clear_required = discord.ui.Button(
            label="清空必要身分組",
            style=discord.ButtonStyle.secondary,
        )
        clear_excluded = discord.ui.Button(
            label="清空排除身分組",
            style=discord.ButtonStyle.secondary,
        )
        clear_required.callback = self.clear_required_roles
        clear_excluded.callback = self.clear_excluded_roles
        clear_roles_row.add_item(clear_required)
        clear_roles_row.add_item(clear_excluded)
        container.add_item(clear_roles_row)

        finalize_row = discord.ui.ActionRow()
        create_button = discord.ui.Button(
            label="建立抽獎",
            style=discord.ButtonStyle.success,
            disabled=not self.draft.ready,
        )
        reset_button = discord.ui.Button(
            label="全部重設",
            style=discord.ButtonStyle.danger,
        )
        create_button.callback = self.create_giveaway
        reset_button.callback = self.reset_all
        finalize_row.add_item(create_button)
        finalize_row.add_item(reset_button)
        container.add_item(finalize_row)

        container.add_item(discord.ui.TextDisplay("### 準備狀態\n" + self._readiness_text()))
        self.add_item(container)

        preview = discord.ui.Container(accent_color=discord.Colour.green())
        preview.add_item(discord.ui.TextDisplay("## 發布預覽"))
        preview.add_item(discord.ui.TextDisplay(self._preview_text()))
        add_optional_media_gallery(
            preview,
            self.draft.image_url or None,
            description="抽獎預覽附圖",
        )
        self.add_item(preview)

    async def open_basic_modal(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(BasicSettingsModal(self))

    async def open_requirements_modal(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(RequirementSettingsModal(self))

    async def open_cross_guild_modal(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(CrossGuildSettingsModal(self))

    async def use_current_channel(self, interaction: discord.Interaction) -> None:
        self.draft.target_channel_id = interaction.channel_id
        self._rebuild()
        await interaction.response.edit_message(view=self)

    async def select_channel(self, interaction: discord.Interaction) -> None:
        raw_values = (interaction.data or {}).get("values", [])
        self.draft.target_channel_id = int(raw_values[0]) if raw_values else None
        self._rebuild()
        await interaction.response.edit_message(view=self)

    async def select_required_roles(self, interaction: discord.Interaction) -> None:
        raw_values = (interaction.data or {}).get("values", [])
        self.draft.required_role_ids = {int(value) for value in raw_values}
        self._rebuild()
        await interaction.response.edit_message(view=self)

    async def select_excluded_roles(self, interaction: discord.Interaction) -> None:
        raw_values = (interaction.data or {}).get("values", [])
        self.draft.excluded_role_ids = {int(value) for value in raw_values}
        self._rebuild()
        await interaction.response.edit_message(view=self)

    async def clear_required_roles(self, interaction: discord.Interaction) -> None:
        self.draft.required_role_ids.clear()
        self._rebuild()
        await interaction.response.edit_message(view=self)

    async def clear_excluded_roles(self, interaction: discord.Interaction) -> None:
        self.draft.excluded_role_ids.clear()
        self._rebuild()
        await interaction.response.edit_message(view=self)

    async def reset_all(self, interaction: discord.Interaction) -> None:
        self.draft = GiveawayDraft()
        self._rebuild()
        await interaction.response.edit_message(view=self)

    async def create_giveaway(self, interaction: discord.Interaction) -> None:
        if not self.draft.ready:
            await interaction.response.send_message(
                view=PanelLayout(
                    "設定還沒完成",
                    self._readiness_text(),
                    accent_color=discord.Colour.red(),
                ),
                ephemeral=True,
            )
            return

        selected_channel: Optional[Union[discord.abc.GuildChannel, discord.Thread]] = None
        if self.draft.target_channel_id is not None:
            selected_channel = self.guild.get_channel(self.draft.target_channel_id)
            if selected_channel is None:
                selected_channel = self.guild.get_thread(self.draft.target_channel_id)

        post_target = resolve_post_channel(interaction, selected_channel)
        if post_target is None:
            await interaction.response.send_message(
                view=PanelLayout(
                    "頻道不可用",
                    "請選擇文字頻道、討論串或 Forum。",
                    accent_color=discord.Colour.red(),
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        starts_at = now_ts()
        ends_at = starts_at + self.draft.duration_seconds
        giveaway = Giveaway(
            guild_id=self.guild.id,
            channel_id=0,
            message_id=0,
            prize=self.draft.prize,
            winners=max(1, self.draft.winners),
            host_id=interaction.user.id,
            starts_at_unix=starts_at,
            ends_at_unix=ends_at,
            image_url=self.draft.image_url or None,
            custom_message=self.draft.custom_message or None,
            required_guild_ids=set(self.draft.required_guild_ids),
            must_said=self.draft.must_said or None,
            required_role_ids=set(self.draft.required_role_ids),
            excluded_role_ids=set(self.draft.excluded_role_ids),
            min_join_days=max(0, self.draft.min_join_days),
            min_messages=max(0, self.draft.min_messages),
        )

        placeholder = PanelLayout(
            "抽獎建立中",
            "- 正在準備抽獎...",
            accent_color=discord.Colour.blurple(),
            timeout=30,
        )

        sent_channel_id: int
        message: discord.Message

        if isinstance(post_target, discord.ForumChannel):
            thread_name = f"抽獎｜{giveaway.prize}"[:100]
            try:
                created = await post_target.create_thread(name=thread_name)
            except TypeError:
                created = await post_target.create_thread(
                    name=thread_name,
                    auto_archive_duration=4320,
                )
            thread = created.thread
            message = created.message
            sent_channel_id = thread.id
            await message.edit(
                content=None,
                embeds=[],
                attachments=[],
                view=placeholder,
                allowed_mentions=NO_PINGS,
            )
        else:
            message = await post_target.send(view=placeholder, allowed_mentions=NO_PINGS)
            sent_channel_id = post_target.id

        giveaway.channel_id = sent_channel_id
        giveaway.message_id = message.id
        self.bot.add_active_giveaway(giveaway)
        await self.bot.flush_state()
        self.bot.register_countdown(giveaway)

        active_view = ActiveGiveawayLayout(self.bot, message.id)
        await message.edit(
            content=None,
            embeds=[],
            attachments=[],
            view=active_view,
            allowed_mentions=NO_PINGS,
        )
        self.bot.add_view(active_view, message_id=message.id)

        summary = "\n".join(
            [
                f"- 訊息 ID：`{message.id}`",
                f"- 發布位置：{get_channel_label(self.guild, sent_channel_id)}",
                f"- 結束時間：{timestamp_short(ends_at)} ({timestamp_short(ends_at, 'R')})",
            ]
        )

        if self.message is not None:
            self.stop()
            await self.message.edit(
                view=PanelLayout(
                    "這份設定已送出",
                    summary + "\n- 若要再開新的抽獎，重新使用 `/gstart`。",
                    accent_color=discord.Colour.green(),
                )
            )

        await interaction.followup.send(
            view=PanelLayout(
                "抽獎已建立",
                summary,
                accent_color=discord.Colour.green(),
            ),
            ephemeral=True,
        )


class GiveawayBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(
            command_prefix=commands.when_mentioned_or("!"),
            intents=intents,
            help_command=None,
        )
        self.giveaways: Dict[int, Giveaway] = {}
        self.archived_giveaways: Dict[int, Giveaway] = {}
        self.giveaways_by_guild: Dict[int, Set[int]] = {}
        self.countdown_tasks: Dict[int, asyncio.Task[None]] = {}
        self.message_sync_tasks: Dict[int, asyncio.Task[None]] = {}
        self.message_sync_pending: Set[int] = set()
        self.message_sync_last_at: Dict[int, float] = {}
        self.message_render_signatures: Dict[int, tuple[int, int]] = {}
        self.channel_message_edit_locks: Dict[int, asyncio.Lock] = {}
        self.channel_message_edit_last_at: Dict[int, float] = {}
        self.state_flush_task: Optional[asyncio.Task[None]] = None
        self.state_flush_pending = False
        self.persistence_lock = asyncio.Lock()
        self.save_path = GIVEAWAY_FILE

    def save_giveaways(self) -> None:
        self.schedule_state_flush()

    def load_giveaways(self) -> None:
        if not self.save_path.exists():
            self.giveaways = {}
            self.archived_giveaways = {}
            self.giveaways_by_guild = {}
            return

        try:
            raw = json.loads(self.save_path.read_text(encoding="utf-8"))
        except Exception as exc:
            LOG.warning("讀取抽獎資料失敗: %s", exc)
            return

        active: Dict[int, Giveaway] = {}
        archived: Dict[int, Giveaway] = {}
        for key, value in raw.items():
            try:
                giveaway = giveaway_from_dict(value)
                if giveaway.ended:
                    archived[int(key)] = giveaway
                else:
                    active[int(key)] = giveaway
            except Exception as exc:
                LOG.warning("解析抽獎資料失敗 (message_id=%s): %s", key, exc)

        self.giveaways = active
        self.archived_giveaways = archived
        self.rebuild_giveaway_index()

    def load_archived_giveaways(self) -> Dict[int, Giveaway]:
        return dict(self.archived_giveaways)

    def rebuild_giveaway_index(self) -> None:
        indexed: Dict[int, Set[int]] = {}
        for message_id, giveaway in self.giveaways.items():
            indexed.setdefault(giveaway.guild_id, set()).add(message_id)
        self.giveaways_by_guild = indexed

    def add_active_giveaway(self, giveaway: Giveaway) -> None:
        self.giveaways[giveaway.message_id] = giveaway
        self.giveaways_by_guild.setdefault(giveaway.guild_id, set()).add(giveaway.message_id)

    def archive_giveaway(self, giveaway: Giveaway) -> None:
        self.giveaways.pop(giveaway.message_id, None)
        guild_bucket = self.giveaways_by_guild.get(giveaway.guild_id)
        if guild_bucket is not None:
            guild_bucket.discard(giveaway.message_id)
            if not guild_bucket:
                self.giveaways_by_guild.pop(giveaway.guild_id, None)
        self.archived_giveaways[giveaway.message_id] = giveaway
        self.message_sync_last_at.pop(giveaway.message_id, None)
        self.message_render_signatures.pop(giveaway.message_id, None)

    def build_state_payload(self) -> dict:
        merged: Dict[str, dict] = {}
        for message_id, giveaway in self.archived_giveaways.items():
            merged[str(message_id)] = giveaway_to_dict(giveaway)
        for message_id, giveaway in self.giveaways.items():
            merged[str(message_id)] = giveaway_to_dict(giveaway)
        return merged

    async def flush_state(self) -> None:
        payload = self.build_state_payload()
        async with self.persistence_lock:
            try:
                await asyncio.to_thread(write_json_atomic, self.save_path, payload)
            except Exception as exc:
                LOG.warning("儲存抽獎資料失敗: %s", exc)

    def schedule_state_flush(self, *, delay: float = STATE_FLUSH_DEBOUNCE_SECONDS) -> None:
        self.state_flush_pending = True
        if self.state_flush_task is not None and not self.state_flush_task.done():
            return
        self.state_flush_task = asyncio.create_task(self._coalesced_state_flush(delay))

    async def _coalesced_state_flush(self, delay: float) -> None:
        try:
            while True:
                await asyncio.sleep(delay)
                self.state_flush_pending = False
                await self.flush_state()
                if not self.state_flush_pending:
                    break
        except asyncio.CancelledError:
            raise
        finally:
            self.state_flush_task = None

    def register_countdown(self, giveaway: Giveaway) -> None:
        existing = self.countdown_tasks.pop(giveaway.message_id, None)
        if existing is not None:
            existing.cancel()

        task = asyncio.create_task(self._countdown_and_end(giveaway.message_id))
        self.countdown_tasks[giveaway.message_id] = task

    def schedule_giveaway_message_sync(
        self,
        message_id: int,
        *,
        delay: float = GIVEAWAY_SYNC_DEBOUNCE_SECONDS,
    ) -> None:
        if message_id not in self.giveaways:
            return

        self.message_sync_pending.add(message_id)
        task = self.message_sync_tasks.get(message_id)
        if task is not None and not task.done():
            return

        loop = asyncio.get_running_loop()
        now = loop.time()
        last_sync_at = self.message_sync_last_at.get(message_id)
        if last_sync_at is None or now - last_sync_at >= GIVEAWAY_SYNC_MIN_INTERVAL_SECONDS:
            initial_delay = GIVEAWAY_SYNC_IDLE_DELAY_SECONDS
        else:
            initial_delay = max(delay, GIVEAWAY_SYNC_MIN_INTERVAL_SECONDS - (now - last_sync_at))

        self.message_sync_tasks[message_id] = asyncio.create_task(
            self._coalesced_sync_giveaway_message(message_id, initial_delay)
        )

    async def _coalesced_sync_giveaway_message(
        self,
        message_id: int,
        delay: float,
    ) -> None:
        try:
            next_delay = delay
            while message_id in self.giveaways:
                if next_delay > 0:
                    await asyncio.sleep(next_delay)
                self.message_sync_pending.discard(message_id)
                synced = await self.sync_giveaway_message(message_id)
                if not synced:
                    self.message_sync_pending.add(message_id)
                    next_delay = GIVEAWAY_SYNC_RETRY_DELAY_SECONDS
                    continue
                self.message_sync_last_at[message_id] = asyncio.get_running_loop().time()
                if message_id not in self.message_sync_pending:
                    break
                next_delay = GIVEAWAY_SYNC_DEBOUNCE_SECONDS
        except asyncio.CancelledError:
            raise
        finally:
            self.message_sync_pending.discard(message_id)
            current = self.message_sync_tasks.get(message_id)
            if current is asyncio.current_task():
                self.message_sync_tasks.pop(message_id, None)

    async def get_member_from_guild(
        self,
        guild: discord.Guild,
        user_id: int,
        *,
        allow_fetch: bool = True,
    ) -> Optional[discord.Member]:
        member = guild.get_member(user_id)
        if member is not None or not allow_fetch:
            return member

        try:
            return await guild.fetch_member(user_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    async def get_join_failure_reasons(
        self,
        member: discord.Member,
        giveaway: Giveaway,
    ) -> List[str]:
        reasons = get_requirement_failure_reasons(member, giveaway, bot=self)

        if giveaway.required_guild_ids:
            reasons = [reason for reason in reasons if not reason.startswith("尚未加入必要群組")]
            for guild_id in giveaway.required_guild_ids:
                external_guild = self.get_guild(guild_id)
                if external_guild is None:
                    reasons.append(f"無法驗證必要群組 `{guild_id}`。")
                    continue
                external_member = await self.get_member_from_guild(external_guild, member.id)
                if external_member is None:
                    reasons.append(f"尚未加入必要群組 `{guild_id}`。")

        return reasons

    async def collect_eligible_user_ids(
        self,
        guild: Optional[discord.Guild],
        giveaway: Giveaway,
    ) -> Set[int]:
        if guild is None:
            return set()

        eligible: Set[int] = set()
        for user_id in giveaway.reacted_users:
            member = await self.get_member_from_guild(guild, user_id)
            if member is None or member.bot:
                continue
            reasons = await self.get_join_failure_reasons(member, giveaway)
            if not reasons:
                eligible.add(user_id)
        return eligible

    async def _startup_sync_active_giveaway_message(
        self,
        message_id: int,
        semaphore: asyncio.Semaphore,
    ) -> None:
        async with semaphore:
            try:
                await self.sync_giveaway_message(message_id)
            except Exception as exc:
                LOG.warning("啟動時同步抽獎訊息失敗 (%s): %s", message_id, exc)

    async def edit_message_with_channel_throttle(
        self,
        channel: RuntimeChannel,
        message_id: int,
        *,
        view: discord.ui.LayoutView,
    ) -> bool:
        lock = self.channel_message_edit_locks.setdefault(channel.id, asyncio.Lock())
        async with lock:
            loop = asyncio.get_running_loop()
            last_edit_at = self.channel_message_edit_last_at.get(channel.id)
            if last_edit_at is not None:
                remaining = CHANNEL_MESSAGE_EDIT_MIN_INTERVAL_SECONDS - (loop.time() - last_edit_at)
                if remaining > 0:
                    await asyncio.sleep(remaining)

            message = channel.get_partial_message(message_id)
            try:
                await message.edit(
                    content=None,
                    embeds=[],
                    attachments=[],
                    view=view,
                    allowed_mentions=NO_PINGS,
                )
            except Exception as exc:
                LOG.warning("同步抽獎訊息失敗 (%s): %s", message_id, exc)
                return False

            self.channel_message_edit_last_at[channel.id] = loop.time()
            return True

    async def setup_hook(self) -> None:
        self.load_giveaways()
        sync_jobs = []
        semaphore = asyncio.Semaphore(STARTUP_SYNC_CONCURRENCY)
        for giveaway in self.giveaways.values():
            self.register_countdown(giveaway)
            try:
                self.add_view(ActiveGiveawayLayout(self, giveaway.message_id), message_id=giveaway.message_id)
            except Exception as exc:
                LOG.warning("註冊抽獎互動元件失敗 (%s): %s", giveaway.message_id, exc)
            sync_jobs.append(self._startup_sync_active_giveaway_message(giveaway.message_id, semaphore))
        if sync_jobs:
            await asyncio.gather(*sync_jobs)
        await self.tree.sync()

    async def sync_giveaway_message(self, message_id: int) -> bool:
        giveaway = self.giveaways.get(message_id)
        if giveaway is None:
            return False

        guild = self.get_guild(giveaway.guild_id)
        channel = resolve_runtime_channel(guild, giveaway.channel_id)
        if channel is None:
            return False

        signature = giveaway_active_render_signature(guild, giveaway, bot=self)
        if self.message_render_signatures.get(message_id) == signature:
            return True

        view = ActiveGiveawayLayout(self, message_id)
        synced = await self.edit_message_with_channel_throttle(
            channel,
            message_id,
            view=view,
        )
        if not synced:
            return False
        self.message_render_signatures[message_id] = signature
        return True

    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return

        content = message.content or ""
        created_ts = message.created_at.timestamp() if message.created_at else now_ts()
        changed_any = False
        author_member = message.author if isinstance(message.author, discord.Member) else None

        message_ids = list(self.giveaways_by_guild.get(message.guild.id, set()))
        for message_id in message_ids:
            giveaway = self.giveaways.get(message_id)
            if giveaway is None or giveaway.ended:
                continue
            if not (giveaway.starts_at_unix <= created_ts <= giveaway.ends_at_unix):
                continue
            if not giveaway.must_said and giveaway.min_messages <= 0:
                continue

            changed = False
            was_eligible = False
            if author_member is not None and message.author.id in giveaway.reacted_users:
                was_eligible = member_meets_requirements(author_member, giveaway, bot=self)

            if giveaway.must_said and match_phrase(content, giveaway.must_said):
                if message.author.id not in giveaway.said_users:
                    giveaway.said_users.add(message.author.id)
                    changed = True

            if giveaway.min_messages > 0:
                giveaway.msg_counts[message.author.id] = giveaway.msg_counts.get(message.author.id, 0) + 1
                changed = True

            if changed:
                changed_any = True
                if author_member is not None and message.author.id in giveaway.reacted_users:
                    is_eligible = member_meets_requirements(author_member, giveaway, bot=self)
                    if was_eligible == is_eligible:
                        continue
                    self.schedule_giveaway_message_sync(giveaway.message_id)

        if changed_any:
            self.save_giveaways()

        await self.process_commands(message)

    async def announce_giveaway_result(
        self,
        giveaway: Giveaway,
        winner_ids: Sequence[int],
        *,
        empty_reason: Optional[str] = None,
    ) -> None:
        guild = self.get_guild(giveaway.guild_id)
        channel = resolve_runtime_channel(guild, giveaway.channel_id)
        if channel is None:
            return

        title = "抽獎結果" if winner_ids else "抽獎結束"
        body_lines = [f"- 獎品：**{giveaway.prize}**"]
        if winner_ids:
            body_lines.append(
                "- 得獎者：" + " ".join(get_member_label(guild, user_id) for user_id in winner_ids)
            )
        elif empty_reason:
            body_lines.append(f"- 結果：{empty_reason}")
        if giveaway.custom_message:
            body_lines.append("- 自訂訊息：")
            body_lines.append(giveaway.custom_message)
        body_lines.append(
            "- 條件："
            + " / ".join(
                line.removeprefix("- ")
                for line in giveaway_condition_lines(giveaway, guild, bot=self)
            )
        )

        await channel.send(
            view=PanelLayout(
                title,
                "\n".join(body_lines),
                accent_color=discord.Colour.green() if winner_ids else discord.Colour.red(),
                image_url=giveaway.image_url,
            ),
            allowed_mentions=USER_PINGS if winner_ids else NO_PINGS,
        )

    async def notify_host_and_winners(self, giveaway: Giveaway, winner_ids: Sequence[int]) -> None:
        guild = self.get_guild(giveaway.guild_id)
        host = guild.get_member(giveaway.host_id) if guild else None

        if host is not None:
            try:
                await host.send(
                    view=PanelLayout(
                        "你的抽獎已結束",
                        "\n".join(
                            [
                                f"- 獎品：**{giveaway.prize}**",
                                f"- 抽獎訊息 ID：`{giveaway.message_id}`",
                                "- 得獎者："
                                + (
                                    " ".join(get_member_label(guild, user_id) for user_id in winner_ids)
                                    if winner_ids
                                    else "無"
                                ),
                                (
                                    "- 自訂訊息：\n" + giveaway.custom_message
                                    if giveaway.custom_message
                                    else "- 自訂訊息：未設定"
                                ),
                            ]
                        ),
                        accent_color=discord.Colour.blue(),
                        image_url=giveaway.image_url,
                    )
                )
            except Exception:
                pass

        for user_id in winner_ids:
            member = guild.get_member(user_id) if guild else None
            if member is None:
                continue
            try:
                await member.send(
                    view=PanelLayout(
                        "你抽中了",
                        "\n".join(
                            [
                                f"- 伺服器：**{guild.name}**" if guild else "- 伺服器：未知",
                                f"- 獎品：**{giveaway.prize}**",
                                f"- 抽獎訊息 ID：`{giveaway.message_id}`",
                                (
                                    "- 自訂訊息：\n" + giveaway.custom_message
                                    if giveaway.custom_message
                                    else "- 自訂訊息：未設定"
                                ),
                            ]
                        ),
                        accent_color=discord.Colour.gold(),
                        image_url=giveaway.image_url,
                    )
                )
            except Exception:
                pass

    async def _end_giveaway(self, message_id: int, *, force: bool = False) -> Optional[List[int]]:
        giveaway = self.giveaways.get(message_id)
        if giveaway is None or giveaway.ended:
            return None

        guild = self.get_guild(giveaway.guild_id)
        channel = resolve_runtime_channel(guild, giveaway.channel_id)
        pool = list(await self.collect_eligible_user_ids(guild, giveaway))

        if pool:
            selected_winners = random.sample(pool, k=min(giveaway.winners, len(pool)))
            empty_reason = None
        else:
            selected_winners = []
            empty_reason = "沒有人符合資格"

        giveaway.ended = True
        giveaway.winner_ids = list(selected_winners)
        self.archive_giveaway(giveaway)
        await self.flush_state()

        if channel is not None:
            try:
                await self.edit_message_with_channel_throttle(
                    channel,
                    giveaway.message_id,
                    view=EndedGiveawayLayout(
                        guild,
                        giveaway,
                        bot=self,
                        winner_ids=selected_winners,
                        empty_reason=empty_reason,
                    ),
                )
            except Exception as exc:
                LOG.warning("更新已結束抽獎訊息失敗 (%s): %s", giveaway.message_id, exc)

        await self.announce_giveaway_result(giveaway, selected_winners, empty_reason=empty_reason)
        await self.notify_host_and_winners(giveaway, selected_winners)

        task = self.countdown_tasks.pop(message_id, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

        sync_task = self.message_sync_tasks.pop(message_id, None)
        if sync_task is not None and sync_task is not asyncio.current_task():
            sync_task.cancel()
        self.message_sync_pending.discard(message_id)

        if not force:
            LOG.info("抽獎已自然結束: %s", message_id)
        return selected_winners

    async def _countdown_and_end(self, message_id: int) -> None:
        giveaway = self.giveaways.get(message_id)
        if giveaway is None or giveaway.ended:
            return

        delay = max(0, giveaway.ends_at_unix - now_ts())
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return

        await self._end_giveaway(message_id)


bot = GiveawayBot()


async def active_giveaway_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> List[app_commands.Choice[str]]:
    if interaction.guild_id is None:
        return []

    current_text = current.strip().lower()
    choices: List[app_commands.Choice[str]] = []
    for giveaway in bot.giveaways.values():
        if giveaway.guild_id != interaction.guild_id:
            continue
        label = f"{giveaway.prize} | {giveaway.message_id}"
        if current_text and current_text not in label.lower():
            continue
        choices.append(app_commands.Choice(name=label[:100], value=str(giveaway.message_id)))
    return choices[:25]


async def any_giveaway_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> List[app_commands.Choice[str]]:
    if interaction.guild_id is None:
        return []

    current_text = current.strip().lower()
    merged = {**bot.load_archived_giveaways(), **bot.giveaways}
    choices: List[app_commands.Choice[str]] = []
    for giveaway in merged.values():
        if giveaway.guild_id != interaction.guild_id:
            continue
        suffix = "進行中" if not giveaway.ended else "已結束"
        label = f"{giveaway.prize} | {giveaway.message_id} | {suffix}"
        if current_text and current_text not in label.lower():
            continue
        choices.append(app_commands.Choice(name=label[:100], value=str(giveaway.message_id)))
    return choices[:25]


def ensure_manage_guild(interaction: discord.Interaction) -> bool:
    member = interaction.user
    return isinstance(member, discord.Member) and member.guild_permissions.manage_guild


@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@bot.tree.command(name="gstart", description="建立新的抽獎")
async def gstart(interaction: discord.Interaction) -> None:
    if not ensure_manage_guild(interaction):
        await interaction.response.send_message(
            view=PanelLayout(
                "權限不足",
                "需要 `管理伺服器` 權限才能建立抽獎。",
                accent_color=discord.Colour.red(),
            ),
            ephemeral=True,
        )
        return

    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            view=PanelLayout(
                "只能在伺服器使用",
                "請在伺服器頻道內使用這個指令。",
                accent_color=discord.Colour.red(),
            ),
            ephemeral=True,
        )
        return

    view = GiveawaySetupView(bot, guild, interaction.user.id)
    await interaction.response.send_message(view=view, ephemeral=True)
    view.bind_message(await interaction.original_response())


@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@bot.tree.command(name="glist", description="查看目前進行中的抽獎")
async def glist(interaction: discord.Interaction) -> None:
    if not ensure_manage_guild(interaction):
        await interaction.response.send_message(
            view=PanelLayout(
                "權限不足",
                "需要 `管理伺服器` 權限才能查看抽獎清單。",
                accent_color=discord.Colour.red(),
            ),
            ephemeral=True,
        )
        return

    guild = interaction.guild
    active = [g for g in bot.giveaways.values() if guild and g.guild_id == guild.id]
    if not active:
        await interaction.response.send_message(
            view=PanelLayout(
                "沒有進行中的抽獎",
                "- 目前這個伺服器沒有進行中的抽獎。",
                accent_color=discord.Colour.orange(),
            ),
            ephemeral=True,
        )
        return

    active.sort(key=lambda g: g.ends_at_unix)
    body = "\n".join(
        [
            f"- `{g.message_id}` | **{g.prize}** | {get_channel_label(guild, g.channel_id)} | {timestamp_short(g.ends_at_unix, 'R')}"
            for g in active[:20]
        ]
    )
    await interaction.response.send_message(
        view=PanelLayout(
            "進行中的抽獎",
            body,
            accent_color=discord.Colour.blurple(),
        ),
        ephemeral=True,
    )


@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@bot.tree.command(name="gend", description="提前結束抽獎")
@app_commands.describe(message_id="抽獎訊息 ID")
@app_commands.autocomplete(message_id=active_giveaway_autocomplete)
async def gend(interaction: discord.Interaction, message_id: str) -> None:
    if not ensure_manage_guild(interaction):
        await interaction.response.send_message(
            view=PanelLayout(
                "權限不足",
                "需要 `管理伺服器` 權限才能結束抽獎。",
                accent_color=discord.Colour.red(),
            ),
            ephemeral=True,
        )
        return

    try:
        giveaway_id = int(message_id)
    except ValueError:
        await interaction.response.send_message(
            view=PanelLayout(
                "ID 格式錯誤",
                "請輸入正確的抽獎訊息 ID。",
                accent_color=discord.Colour.red(),
            ),
            ephemeral=True,
        )
        return

    winners = await bot._end_giveaway(giveaway_id, force=True)
    if winners is None:
        await interaction.response.send_message(
            view=PanelLayout(
                "找不到抽獎",
                "這個抽獎不存在，或是已經結束。",
                accent_color=discord.Colour.red(),
            ),
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        view=PanelLayout(
            "抽獎已結束",
            f"- 抽獎訊息 ID：`{giveaway_id}`\n- 得獎人數：**{len(winners)}**",
            accent_color=discord.Colour.green(),
        ),
        ephemeral=True,
    )


@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@bot.tree.command(name="greroll", description="重抽已結束或進行中的抽獎")
@app_commands.describe(
    message_id="抽獎訊息 ID",
    winners="重新抽出的得獎人數",
)
@app_commands.autocomplete(message_id=any_giveaway_autocomplete)
async def greroll(
    interaction: discord.Interaction,
    message_id: str,
    winners: app_commands.Range[int, 1, 50] = 1,
) -> None:
    if not ensure_manage_guild(interaction):
        await interaction.response.send_message(
            view=PanelLayout(
                "權限不足",
                "需要 `管理伺服器` 權限才能重抽。",
                accent_color=discord.Colour.red(),
            ),
            ephemeral=True,
        )
        return

    try:
        giveaway_id = int(message_id)
    except ValueError:
        await interaction.response.send_message(
            view=PanelLayout(
                "ID 格式錯誤",
                "請輸入正確的抽獎訊息 ID。",
                accent_color=discord.Colour.red(),
            ),
            ephemeral=True,
        )
        return

    giveaway = bot.giveaways.get(giveaway_id) or bot.load_archived_giveaways().get(giveaway_id)
    if giveaway is None:
        await interaction.response.send_message(
            view=PanelLayout(
                "找不到抽獎",
                "查無對應的抽獎資料。",
                accent_color=discord.Colour.red(),
            ),
            ephemeral=True,
        )
        return

    guild = bot.get_guild(giveaway.guild_id)
    if guild is None:
        await interaction.response.send_message(
            view=PanelLayout(
                "伺服器不可用",
                "目前無法取得這個抽獎所在的伺服器快取。",
                accent_color=discord.Colour.red(),
            ),
            ephemeral=True,
        )
        return

    pool = list(await bot.collect_eligible_user_ids(guild, giveaway))
    previous_winners = set(giveaway.winner_ids)
    if previous_winners:
        pool = [user_id for user_id in pool if user_id not in previous_winners]
    if not pool:
        await interaction.response.send_message(
            view=PanelLayout(
                "沒有可重抽的人",
                "目前沒有任何可重抽的合格參加者，可能都已經中獎過了。",
                accent_color=discord.Colour.orange(),
            ),
            ephemeral=True,
        )
        return

    selected = random.sample(pool, k=min(winners, len(pool)))
    if selected and giveaway.ended:
        giveaway.winner_ids.extend(user_id for user_id in selected if user_id not in giveaway.winner_ids)
        bot.archived_giveaways[giveaway.message_id] = giveaway
        await bot.flush_state()
    await interaction.response.send_message(
        view=PanelLayout(
            "重抽結果",
            "\n".join(
                [
                    f"- 獎品：**{giveaway.prize}**",
                    "- 得獎者：" + " ".join(get_member_label(guild, user_id) for user_id in selected),
                    f"- 原抽獎訊息 ID：`{giveaway.message_id}`",
                ]
            ),
            accent_color=discord.Colour.green(),
        ),
        allowed_mentions=USER_PINGS,
    )


@bot.event
async def on_ready() -> None:
    await bot.change_presence(activity=discord.Game(name="Giveaway Bot V2"))
    LOG.info("Bot 已上線：%s", bot.user)


if __name__ == "__main__":
    token = os.getenv("TOKEN", "")
    if not token:
        LOG.error("找不到 TOKEN，請先在 .env 設定。")
    else:
        bot.run(token)
