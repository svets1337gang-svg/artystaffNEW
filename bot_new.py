import asyncio
import logging
import os

import discord
from discord import app_commands
from discord.ext import commands

from config import (
    DISCORD_TOKEN, DISCORD_GUILD_ID,
    FT_GOOGLE_SHEETS_ID, FT_SHEET_NAME,
    RW_GOOGLE_SHEETS_ID, RW_SHEET_NAME,
)
from google_manager import (
    find_user_by_nick, list_nicks, collect_staff, division_ids,
    normalize_nick, punishment_limits, describe_google_error,
)
from style import BotStyle
from ui_components import PERSISTENT_VIEWS, build_profile_embed, StaffMainView

# ===== НАСТРОЙКИ ЛОГИРОВАНИЯ =====
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('StaffBot')
# =========================================================


# =========================================================
# ЯДРО БОТА
# =========================================================
class StaffBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        # members-интент был нужен только аудиту доступов (guild.members);
        # после удаления /audit_access бот обходится дефолтными + message_content.
        super().__init__(command_prefix='/', intents=intents)

    async def setup_hook(self):
        # Persistent views: регистрируются здесь, чтобы кнопки продолжали
        # работать после перезапуска бота. Состояние (ник+дивизион) зашито
        # в заголовок эмбеда и перечитывается при каждом нажатии — поэтому
        # достаточно одного экземпляра каждого view.
        # PERSISTENT_VIEWS хранит классы — инстанцируем здесь, внутри loop'а.
        for view_cls in PERSISTENT_VIEWS:
            self.add_view(view_cls())

        # Синк по гильдии применяется мгновенно, глобальный Discord
        # раскидывает до часа. Если DISCORD_GUILD_ID не задан — глобально.
        if DISCORD_GUILD_ID:
            guild = discord.Object(id=int(DISCORD_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            # Копии команд в дереве + очистка глобального набора: если бот
            # раньше синкался глобально, без этого старые глобальные команды
            # останутся в Discord и продублируют гильдейские.
            self.tree.clear_commands(guild=None)
            await self.tree.sync(guild=guild)
            await self.tree.sync()
            logger.info("Commands synced to guild %s (global set cleared).", DISCORD_GUILD_ID)
        else:
            await self.tree.sync()
            logger.info("Commands synced globally.")


bot = StaffBot()


# ===== ЛОГИРОВАНИЕ ДЕЙСТВИЙ =====
async def log_action(user, action_text: str):
    """Пишет лог действия в канал из переменной окружения LOG_CHANNEL_ID.

    Принимает discord.User/Member напрямую (а не interaction), чтобы функцию
    можно было вызывать и из событий бота, где interaction не существует.
    """
    log_channel_id = os.getenv('LOG_CHANNEL_ID')
    if not log_channel_id:
        return
    try:
        channel = bot.get_channel(int(log_channel_id))
        if channel:
            embed = BotStyle.create_embed(
                "📝 Лог действий",
                f"**Пользователь:** {user.mention}\n**Действие:** {action_text}",
                color=discord.Color.light_grey()
            )
            await channel.send(embed=embed)
    except Exception as e:
        logger.error(f"Logging error: {e}")


# ===== COMMANDS =====

async def nick_autocomplete(interaction: discord.Interaction, current: str):
    """Автокомплит ников сотрудников обеих таблиц для /staff.

    Discord вызывает его на каждый символ ввода и ждёт ответа за 3 секунды,
    поэтому список берётся из кэша таблиц (TTL), а не из API напрямую.
    """
    def collect():
        ft = [(n, 'FT') for n in list_nicks(FT_GOOGLE_SHEETS_ID, FT_SHEET_NAME)]
        rw = [(n, 'RW') for n in list_nicks(RW_GOOGLE_SHEETS_ID, RW_SHEET_NAME)]
        return ft + rw

    try:
        entries = await asyncio.to_thread(collect)
    except Exception as e:
        logger.error(f"Autocomplete error: {e}")
        return []

    query = (current or '').strip().lower()
    matches = []
    seen = set()
    for nick, div in entries:
        if not query or query in nick.lower():
            key = nick.lower()
            if key in seen:
                continue  # Discord требует уникальных value в choices
            seen.add(key)
            # app_commands.Choice в discord.py 2.3.2 НЕ поддерживает
            # description (только name/value), поэтому дивизион показываем
            # прямо в имени, а value оставляем чистым ником для /staff.
            matches.append(app_commands.Choice(name=f"{nick} • {div}", value=nick))
        if len(matches) >= 25:  # жёсткий лимит Discord
            break
    return matches


@bot.tree.command(name="staff", description="Центр управления сотрудником")
@app_commands.guild_only()
@app_commands.describe(nick="Ник сотрудника")
@app_commands.autocomplete(nick=nick_autocomplete)
async def staff_command(interaction: discord.Interaction, nick: str):
    # Мгновенно отвечаем Дискорду, чтобы избежать ошибки "Unknown interaction".
    # Ответ ephemeral — карточка сотрудника видна только вызвавшему.
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.errors.NotFound:
        return

    # Поиск пользователя в таблицах: сначала FT, затем RW.
    # reraise=True разделяет «таблица недоступна» (GoogleAPIError с
    # конкретным текстом) и «сотрудника нет в таблице» (None) — раньше
    # обе ситуации показывались как «не найден».
    try:
        user_data = await asyncio.to_thread(
            find_user_by_nick, nick, FT_GOOGLE_SHEETS_ID, FT_SHEET_NAME, reraise=True)
        division = 'FT'
        if not user_data:
            division = 'RW'
            user_data = await asyncio.to_thread(
                find_user_by_nick, nick, RW_GOOGLE_SHEETS_ID, RW_SHEET_NAME, reraise=True)
    except Exception as e:
        logger.error(f"/staff lookup failed: {e}", exc_info=True)
        await interaction.followup.send(describe_google_error(e), ephemeral=True)
        return

    if not user_data:
        await interaction.followup.send(
            f"❌ Сотрудник `{nick}` не найден ни в одном из дивизионов.",
            ephemeral=True
        )
        return

    # Заголовок эмбеда содержит ник+дивизион — по нему кнопки (persistent
    # views) восстановят контекст при любом нажатии, в т.ч. после рестарта.
    embed = build_profile_embed(user_data, division)
    await interaction.followup.send(embed=embed, view=StaffMainView(), ephemeral=True)


# ===== СТАТИСТИКА (Ф3) =====

DIVISION_CHOICES = [
    app_commands.Choice(name="FT", value="FT"),
    app_commands.Choice(name="RW", value="RW"),
]


def _collect_staff_safe(division):
    """collect_staff с превращением GoogleAPIError в текст (синхронно)."""
    sheet_id, sheet_name, _ = division_ids(division)
    return collect_staff(sheet_id, sheet_name, reraise=True)


@bot.tree.command(name="top", description="Топ-10 сотрудников по баллам")
@app_commands.guild_only()
@app_commands.choices(division=DIVISION_CHOICES)
@app_commands.describe(division="Дивизион (по умолчанию — оба)")
async def top_command(interaction: discord.Interaction, division: str = None):
    # Аннотация — сырой str, а не Choice[str]: так в колбэк гарантированно
    # приходит значение ('FT'/'RW'), а не объект Choice.
    await interaction.response.defer(ephemeral=True)
    divisions = [division] if division else ['FT', 'RW']

    try:
        sections = []
        for div in divisions:
            staff = await asyncio.to_thread(_collect_staff_safe, div)
            if not staff:
                sections.append(f"**{div}**: сотрудников нет.")
                continue
            ranked = sorted(staff, key=lambda s: s['points'], reverse=True)[:10]
            medals = ['🥇', '🥈', '🥉']
            lines = []
            for i, s in enumerate(ranked):
                prefix = medals[i] if i < len(medals) else f"**{i + 1}.**"
                lines.append(f"{prefix} `{s['nick']}` — **{s['points']:,}** баллов".replace(',', ' '))
            header = f"**Дивизион {div}** (всего сотрудников: {len(staff)})"
            sections.append(header + "\n" + "\n".join(lines))
    except Exception as e:
        logger.error(f"/top failed: {e}", exc_info=True)
        await interaction.followup.send(describe_google_error(e), ephemeral=True)
        return

    embed = BotStyle.create_embed(
        "🏆 Топ-10 по баллам",
        "\n\n".join(sections),
        color=BotStyle.SUCCESS_COLOR,
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


def _match_staff_member(staff, member):
    """Сотрудник таблицы, чей ник совпадает с ником/отображаемым именем участника.

    Сравниваются нормализованные значения без учёта регистра: сначала
    ник участника (name), затем отображаемое имя (display_name).
    """
    for member_attr in ('name', 'display_name'):
        raw = getattr(member, member_attr, None)
        if not raw:
            continue
        target = normalize_nick(raw).lower()
        if not target:
            continue
        for s in staff:
            if normalize_nick(s['nick']).lower() == target:
                return s
    return None


@bot.tree.command(name="stat", description="Моя статистика сотрудника (по нику в Discord)")
@app_commands.guild_only()
async def stat_command(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    member = interaction.user
    try:
        match, division = None, None
        for div in ('FT', 'RW'):
            staff = await asyncio.to_thread(_collect_staff_safe, div)
            match = _match_staff_member(staff, member)
            if match:
                division = div
                break
    except Exception as e:
        logger.error(f"/stat failed: {e}", exc_info=True)
        await interaction.followup.send(describe_google_error(e), ephemeral=True)
        return

    if not match:
        await interaction.followup.send(
            "❌ Вы не найдены в таблицах сотрудников. Проверьте, что ник в Discord "
            "совпадает с ником в таблице (клан-теги вида `[TAG]` игнорируются).",
            ephemeral=True,
        )
        return

    warn_limit, strike_limit = punishment_limits(division)
    at_cap = match['warns'] >= warn_limit
    fields = [
        ("🍀 Должность", match['position'] or '—', True),
        ("💰 Баллы", f"{match['points']:,}".replace(',', ' '), True),
        ("☠ Варны", f"{match['warns']}/{warn_limit}" + (" **(MAX)**" if at_cap else ""), True),
        ("☠ Устники", f"{match['strikes']}/{strike_limit}", True),
    ]
    color = BotStyle.ERROR_COLOR if at_cap else BotStyle.PRIMARY_COLOR
    embed = BotStyle.create_embed(
        f"📈 Статистика • {match['nick']} [{division}]",
        "Ваша карточка сотрудника из таблицы дивизиона "
        f"**{division}** (строка {match['row']}).",
        color=color,
        fields=fields,
    )
    await interaction.followup.send(embed=embed, ephemeral=True)


@bot.event
async def on_ready():
    logger.info("Bot is online: %s (id=%s)", bot.user, bot.user.id)


if __name__ == "__main__":
    # Пустой токен даёт у bot.run() невнятную ошибку LoginFailure —
    # падаем сразу и явно, до подключения к Discord.
    if not DISCORD_TOKEN or not DISCORD_TOKEN.strip():
        raise SystemExit(
            "DISCORD_TOKEN не задан. Укажите его в .env или в переменных окружения."
        )
    bot.run(DISCORD_TOKEN)
