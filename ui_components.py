import asyncio
import logging
import re

import discord

from config import (
    FT_WARN_COST, FT_WARNING_COST,
    RW_WARN_COST, RW_WARNING_COST,
)
from google_manager import (
    google_manager, division_ids, find_user_by_nick,
    parse_points, parse_punishment, punishment_limits, staff_lock,
    describe_google_error, GoogleAPIError,
)
from style import BotStyle

logger = logging.getLogger('StaffBot.UI')

# =========================================================
# СОСТОЯНИЕ В СООБЩЕНИИ
# ---------------------------------------------------------
# discord.py 2.3.2 не поддерживает DynamicItem, а persistent view
# диспатчится по статическому custom_id (одна запись на id). Поэтому
# кнопки НЕ хранят состояние: ник и дивизион зашиты в заголовок эмбеда
# единым форматом и перечитываются из таблицы при каждом нажатии.
# Это же позволяет кнопкам работать после рестарта бота.
# =========================================================

TITLE_PROFILE = "📊 Профиль сотрудника"
TITLE_POINTS = "💰 Управление баллами"
TITLE_PUNISH = "⚠️ Управление наказаниями"
TITLE_ACCESS = "🔑 Управление доступами"

# Формат заголовка: "<метка> • <ник> [<дивизион>]"
_STATE_RE = re.compile(r"•\s*(?P<nick>.+?)\s*\[(?P<div>FT|RW)\]\s*$")


def compose_title(label: str, nick: str, division: str) -> str:
    """Собирает заголовок эмбеда с зашитым состоянием (ник + дивизион)."""
    return f"{label} • {nick} [{division}]"


def parse_state(message):
    """Достаёт (nick, division) из заголовка эмбеда сообщения.

    Возвращает (None, None), если сообщение/эмбед не найдены или
    заголовок не соответствует формату.
    """
    if message is None or not message.embeds:
        return None, None
    title = message.embeds[0].title or ""
    m = _STATE_RE.search(title)
    if not m:
        return None, None
    return m.group('nick').strip(), m.group('div')


def build_profile_embed(user_data, division, note=None, color=None):
    """Карточка сотрудника. Заголовок содержит ник+дивизион для parse_state."""
    fields = [
        ("📧 Почта", user_data.get('email') or 'Не указана', True),
        ("🍀 Должность", user_data.get('position') or '—', True),
        ("💰 Баллы", str(user_data.get('points', '0')), True),
        ("☠ Варны", str(user_data.get('warns', '0/3')), True),
        ("☠ Устники", str(user_data.get('warnings', '0/3')), True),
    ]
    description = f"Дивизион: **{division}**"
    if note:
        description += f"\n\n{note}"
    return BotStyle.create_embed(
        compose_title(TITLE_PROFILE, user_data['nick'], division),
        description,
        color=color or BotStyle.PRIMARY_COLOR,
        fields=fields,
    )


# =========================================================
# СИНХРОННЫЕ ОПЕРАЦИИ С ТАБЛИЦЕЙ (вызываются через to_thread)
# =========================================================

def _reload(nick, division, fresh=False):
    """Перечитывает сотрудника из таблицы дивизиона.

    fresh=True обходит кэш — используется внутри критической секции записи.
    При недоступной таблице поднимает GoogleAPIError с человекочитаемым
    текстом (403/404/квота): None остаётся только для «сотрудник не найден»,
    эти две ситуации больше не путаются.
    """
    sheet_id, sheet_name, _ = division_ids(division)
    return find_user_by_nick(nick, sheet_id, sheet_name, fresh=fresh, reraise=True)


def _safe_reload(nick, division):
    """_reload без проброса ошибки — для мест, где операция уже выполнена.

    В _apply_access права Drive к моменту перечитывания уже изменены:
    ошибка чтения таблицы не должна «отменять» успешную выдачу/отзыв.
    """
    try:
        return _reload(nick, division)
    except GoogleAPIError as e:
        logger.warning(f"Reload after access change failed: {e}")
        return None


def _format_punishment(count: int, limit: int) -> str:
    """Формат ячейки наказания: '2/3', на потолке — '3/3 (MAX)'.

    Метка (MAX) — триггер для руководства: сотрудник набрал потолок
    варнов. parse_punishment читает число до '/', поэтому метка не
    мешает последующим операциям.
    """
    text = f"{count}/{limit}"
    return f"{text} (MAX)" if count >= limit else text


def _apply_points(division, nick, action, amount):
    """Начисление/списание баллов. Возвращает (ok, note, color, updated_data).

    Вызывается ПОД staff_lock() этого сотрудника: чтение fresh=True даёт
    актуальное состояние, запись идёт одним batch-запросом, после чего кэш
    патчится записанным значением — повторный запрос к API не нужен.
    """
    sheet_id, sheet_name, _ = division_ids(division)
    user = _reload(nick, division, fresh=True)
    if user is None:
        return False, f"❌ Сотрудник `{nick}` не найден в таблице {division}.", BotStyle.ERROR_COLOR, None

    current = parse_points(user['points'])
    new_points = (current + amount) if action == 'add' else max(0, current - amount)
    google_manager.write_cells(sheet_id, sheet_name, [(user['row'], 3, str(new_points))])

    # Кэш уже содержит новое значение — обновлённые данные берутся из него.
    updated = _reload(nick, division) or user
    verb = 'выдано' if action == 'add' else 'списано'
    note = f"💰 {nick}: {verb} **{amount}** баллов. Баланс: **{new_points}**."
    color = BotStyle.SUCCESS_COLOR if action == 'add' else BotStyle.WARNING_COLOR
    return True, note, color, updated


def _apply_punishment(division, nick, punish_type, action, amount):
    """Выдача/снятие наказания. Возвращает (ok, note, color, updated_data).

    Все изменения строки (наказание, баллы, варн при конвертации устников)
    накапливаются и пишутся ОДНИМ batch-запросом под staff_lock().

    Политика наказаний (Ф6): потолки берутся из config (WARN_LIMIT,
    STRIKE_LIMIT на дивизион). Выдача сверх потолка варнов отклоняется;
    достижение потолка помечает ячейку «(MAX)» и добавляет в note алерт —
    модалка пишет его в лог-канал. strike_limit устников конвертируются
    в +1 варн, если конверсия пробила бы потолок варнов — тоже отказ.
    """
    sheet_id, sheet_name, _ = division_ids(division)
    user = _reload(nick, division, fresh=True)
    if user is None:
        return False, f"❌ Сотрудник `{nick}` не найден в таблице {division}.", BotStyle.ERROR_COLOR, None

    warn_limit, strike_limit = punishment_limits(division)
    row = user['row']
    col = 4 if punish_type == 'warn' else 5
    field = 'warns' if punish_type == 'warn' else 'warnings'
    current = parse_punishment(user[field])
    warn_count = parse_punishment(user['warns'])
    ru = 'варн' if punish_type == 'warn' else 'устник'
    updates = []
    alert = ""

    if action == 'add':
        if punish_type == 'warn':
            new_count = current + amount
            if new_count > warn_limit:
                return False, (f"❌ Отказано: у `{nick}` уже {current}/{warn_limit} варнов "
                               f"(потолок дивизиона {division}). Больше выдать нельзя."), \
                    BotStyle.ERROR_COLOR, user
            updates.append((row, col, _format_punishment(new_count, warn_limit)))
            if new_count >= warn_limit:
                alert = (f"⛔ ВНИМАНИЕ: `{nick}` [{division}] набрал ПОТОЛОК варнов "
                         f"({new_count}/{warn_limit})!")
        else:  # strike
            new_count = current + amount
            # strike_limit устников автоматически конвертируются в +1 варн
            extra_warns, new_count = divmod(new_count, strike_limit)
            if extra_warns:
                if warn_count + extra_warns > warn_limit:
                    return False, (f"❌ Отказано: конвертация {extra_warns} устник(ов) в варн "
                                   f"превысит потолок варнов `{nick}` "
                                   f"({warn_count}/{warn_limit})."), \
                        BotStyle.ERROR_COLOR, user
                updates.append((row, 4, _format_punishment(warn_count + extra_warns, warn_limit)))
                if warn_count + extra_warns >= warn_limit:
                    alert = (f"⛔ ВНИМАНИЕ: `{nick}` [{division}] набрал ПОТОЛОК варнов "
                             f"({warn_count + extra_warns}/{warn_limit}) — конвертация из устников!")
            updates.append((row, col, _format_punishment(new_count, strike_limit)))
        note = f"⚠️ Выдано {amount} {ru}(ов)."
        if alert:
            note += f"\n\n{alert}"
        color = BotStyle.ERROR_COLOR if alert else BotStyle.WARNING_COLOR
    else:
        if current < amount:
            return False, "❌ Недостаточно наказаний для снятия.", BotStyle.ERROR_COLOR, user
        cost = (FT_WARN_COST if punish_type == 'warn' else FT_WARNING_COST) if division == 'FT' \
            else (RW_WARN_COST if punish_type == 'warn' else RW_WARNING_COST)
        points = parse_points(user['points'])
        if points < cost:
            return False, f"❌ Недостаточно баллов! Требуется: {cost}.", BotStyle.ERROR_COLOR, user
        limit = warn_limit if punish_type == 'warn' else strike_limit
        updates.append((row, col, _format_punishment(current - amount, limit)))
        updates.append((row, 3, str(points - cost)))
        note = f"✅ Снято {amount} {ru}(ов). Списано {cost} баллов."
        color = BotStyle.SUCCESS_COLOR

    google_manager.write_cells(sheet_id, sheet_name, updates)
    updated = _safe_reload(nick, division) or user
    return True, note, color, updated


def _apply_access(division, nick, action, email):
    """Выдача/отзыв доступа к таблице и форме. Возвращает (ok, note, color, updated)."""
    sheet_id, _, form_id = division_ids(division)
    email = email.strip().lower()
    service = google_manager.get_drive_service()

    if action == 'grant':
        if service is None:
            return False, "❌ Drive API недоступен (см. лог).", BotStyle.ERROR_COLOR, None
        service.permissions().create(
            fileId=sheet_id,
            body={'type': 'user', 'role': 'writer', 'emailAddress': email},
        ).execute()
        # Форма: Drive API не работает с Google Forms (403) — только Forms API
        try:
            google_manager.add_form_permission(form_id, email)
        except Exception as e:
            logger.error(f"Error granting form access ({form_id}): {e}", exc_info=True)
            return True, (f"Доступ к **таблице** выдан для {email}\n"
                          f"⚠️ Доступ к **форме** выдать не удалось. "
                          f"Подробности в логе бота."), BotStyle.ERROR_COLOR, _safe_reload(nick, division)
        return True, f"🔑 Доступ **выдан** для {email} (таблица и форма).", BotStyle.SUCCESS_COLOR, _safe_reload(nick, division)

    # revoke
    sheet_ok = True
    sheet_err = ""
    if service is None:
        sheet_ok = False
        sheet_err = "Drive API недоступен (см. лог)"
    else:
        try:
            # revoke_permission перебирает страницы прав Drive (максимум
            # 100 на страницу) — inline-версия без пагинации могла молча
            # «промахнуться» по email на больших таблицах.
            google_manager.revoke_permission(sheet_id, email)
        except Exception as e:
            sheet_ok = False
            sheet_err = describe_google_error(e)
            logger.error(f"Error revoking sheet access ({sheet_id}): {e}", exc_info=True)

    form_ok = True
    form_err = ""
    try:
        google_manager.remove_form_permission(form_id, email)
    except Exception as e:
        form_ok = False
        form_err = describe_google_error(e)
        logger.error(f"Error revoking form access ({form_id}): {e}", exc_info=True)

    updated = _safe_reload(nick, division)
    if sheet_ok and form_ok:
        return True, f"🔑 Доступ **полностью отозван** для {email} (таблица и форма).", BotStyle.WARNING_COLOR, updated
    parts = []
    if not sheet_ok:
        parts.append(f"• таблица — не отозван: {sheet_err}")
    if not form_ok:
        parts.append(f"• форма — не отозван: {form_err}")
    return (True,
            f"⚠️ Доступ отозван **частично** для {email}\n" + "\n".join(parts) +
            "\n\nПодробности в логе бота.",
            BotStyle.ERROR_COLOR, updated)


# =========================================================
# ОБЩИЕ ВСПОМОГАТЕЛЬНЫЕ КОРУТИНЫ
# =========================================================

async def _run_locked(fn, division, nick, *args):
    """Выполняет операцию над сотрудником под его персональным локом.

    Лок (дивизион+ник) делает read-modify-write атомарным: параллельные
    нажатия двух менеджеров сериализуются, и никто не перезапишет чужой
    результат. Исключения Google API превращаются в понятный ответ —
    иначе они ушли бы в диспатчер discord.py («This interaction failed»).

    Возвращает (ok, note, color, updated).
    """
    async with staff_lock(division, nick):
        try:
            return await asyncio.to_thread(fn, division, nick, *args)
        except Exception as e:
            logger.error(f"{fn.__name__} failed for {nick} ({division}): {e}", exc_info=True)
            # GoogleAPIError уже несёт готовый для менеджера текст; прочие
            # исключения Google API расшифровывает describe_google_error.
            return False, describe_google_error(e), BotStyle.ERROR_COLOR, None


async def _state_or_error(interaction):
    """Парсит (nick, division) из сообщения; при неудаче отвечает ошибкой."""
    nick, division = parse_state(interaction.message)
    if not nick:
        await interaction.response.send_message(
            "❌ Не удалось определить сотрудника. Откройте карточку заново через /staff.",
            ephemeral=True,
        )
        return None, None
    return nick, division


async def _return_to_profile(interaction, nick, division):
    """Возврат к карточке профиля с актуальными данными."""
    await interaction.response.defer()
    try:
        updated = await asyncio.to_thread(_reload, nick, division)
    except Exception as e:
        logger.error(f"Reload failed in _return_to_profile: {e}", exc_info=True)
        updated = None
        error_text = describe_google_error(e)
    else:
        error_text = f"❌ Сотрудник `{nick}` не найден в таблице {division}."
    if updated is None:
        await interaction.edit_original_response(
            embed=BotStyle.create_embed(
                compose_title(TITLE_PROFILE, nick, division),
                error_text,
                color=BotStyle.ERROR_COLOR,
            ),
            view=StaffMainView(),
        )
        return
    await interaction.edit_original_response(
        embed=build_profile_embed(updated, division),
        view=StaffMainView(),
    )


# =========================================================
# МОДАЛКИ
# ---------------------------------------------------------
# custom_id НЕ задаём вручную: модалки хранятся в глобальном словаре
# по custom_id, ручной id вызвал бы коллизии между сотрудниками.
# Состояние (nick, division) передаётся в конструктор и живёт в памяти
# до submit — это нормально для короткого диалога.
# =========================================================

class StaffPointsModal(discord.ui.Modal):
    def __init__(self, action, nick, division):
        super().__init__(title="Добавление баллов" if action == 'add' else "Списание баллов")
        self.action, self.nick, self.division = action, nick, division
        self.amount_input = discord.ui.TextInput(
            label="Количество баллов", placeholder="Например: 500", required=True)
        self.add_item(self.amount_input)

    async def on_submit(self, interaction):
        try:
            amount = int(self.amount_input.value.strip())
            if amount <= 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                "❌ Введите корректное положительное число.", ephemeral=True)
            return

        # defer() БЕЗ ephemeral создаёт публичный placeholder, и последующий
        # edit_original_response выкладывает профиль сотрудника (в т.ч. почту)
        # в канал — модалка открывалась из приватной карточки.
        await interaction.response.defer(ephemeral=True)
        ok, note, color, updated = await _run_locked(
            _apply_points, self.division, self.nick, self.action, amount)
        if not ok:
            # Правим placeholder, а не шлём followup: иначе в истории
            # остаётся пустое отложенное сообщение.
            await interaction.edit_original_response(content=note)
            return
        await interaction.edit_original_response(
            embed=build_profile_embed(updated, self.division, note=note, color=color),
            view=StaffMainView(),
        )
        from bot_new import log_action
        await log_action(interaction.user,
                         f"[{self.division}] {self.action} points ({amount}) → {self.nick}. Итог: {updated['points']}")


class StaffPunishmentModal(discord.ui.Modal):
    def __init__(self, punish_type, action, nick, division):
        ru = 'варна' if punish_type == 'warn' else 'устника'
        super().__init__(title=f"{'Выдача' if action == 'add' else 'Снятие'} {ru}")
        self.punish_type, self.action, self.nick, self.division = punish_type, action, nick, division
        self.amount_input = discord.ui.TextInput(
            label="Количество", placeholder="1", required=True)
        self.add_item(self.amount_input)

    async def on_submit(self, interaction):
        try:
            amount = int(self.amount_input.value.strip())
            if amount <= 0:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("❌ Введите положительное число.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        ok, note, color, updated = await _run_locked(
            _apply_punishment, self.division, self.nick, self.punish_type, self.action, amount)
        if not ok:
            await interaction.edit_original_response(content=note)
            return
        await interaction.edit_original_response(
            embed=build_profile_embed(updated, self.division, note=note, color=color),
            view=StaffMainView(),
        )
        from bot_new import log_action
        log_text = f"[{self.division}] {self.action} {self.punish_type} ({amount}) → {self.nick}"
        # note несёт алерт «⛔ ВНИМАНИЕ» при достижении потолка варнов —
        # он должен попасть и в лог-канал, а не только в ответ менеджеру.
        if "⛔" in note:
            log_text += f". {note.split('⛔', 1)[1].strip()}"
        await log_action(interaction.user, log_text)


class StaffAccessModal(discord.ui.Modal):
    def __init__(self, action, nick, division, current_email=""):
        super().__init__(title="Отзыв доступа" if action == 'revoke' else "Выдача доступа")
        self.action, self.nick, self.division = action, nick, division
        if action == 'revoke':
            self.email_input = discord.ui.TextInput(
                label="Почта сотрудника",
                default=current_email or "Не указана",
                required=False,
            )
        else:
            self.email_input = discord.ui.TextInput(
                label="Email пользователя",
                placeholder="example@gmail.com",
                required=True, min_length=5, max_length=100,
            )
        self.add_item(self.email_input)

    async def on_submit(self, interaction):
        email = (self.email_input.value or "").strip().lower()
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            await interaction.response.send_message(
                "❌ Некорректный формат почты.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        ok, note, color, updated = await _run_locked(
            _apply_access, self.division, self.nick, self.action, email)
        if not ok or updated is None:
            await interaction.edit_original_response(content=note)
            return
        await interaction.edit_original_response(
            embed=build_profile_embed(updated, self.division, note=note, color=color),
            view=StaffMainView(),
        )
        from bot_new import log_action
        await log_action(interaction.user,
                         f"[{self.division}] {self.action} access for {email} ({self.nick})")


# =========================================================
# PERSISTENT VIEWS (статические custom_id)
# =========================================================

class StaffPointsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Добавить баллы", style=discord.ButtonStyle.green, custom_id="staff_points_add")
    async def add_btn(self, interaction, button):
        nick, division = await _state_or_error(interaction)
        if nick:
            await interaction.response.send_modal(StaffPointsModal('add', nick, division))

    @discord.ui.button(label="Списать баллы", style=discord.ButtonStyle.red, custom_id="staff_points_sub")
    async def sub_btn(self, interaction, button):
        nick, division = await _state_or_error(interaction)
        if nick:
            await interaction.response.send_modal(StaffPointsModal('sub', nick, division))

    @discord.ui.button(label="⬅ Назад", style=discord.ButtonStyle.gray, custom_id="staff_points_back")
    async def back_btn(self, interaction, button):
        nick, division = await _state_or_error(interaction)
        if nick:
            await _return_to_profile(interaction, nick, division)


class StaffPunishmentView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="+ Варн", style=discord.ButtonStyle.danger, custom_id="staff_punish_warn_add")
    async def warn_add(self, interaction, button):
        nick, division = await _state_or_error(interaction)
        if nick:
            await interaction.response.send_modal(StaffPunishmentModal('warn', 'add', nick, division))

    @discord.ui.button(label="- Варн", style=discord.ButtonStyle.gray, custom_id="staff_punish_warn_rem")
    async def warn_rem(self, interaction, button):
        nick, division = await _state_or_error(interaction)
        if nick:
            await interaction.response.send_modal(StaffPunishmentModal('warn', 'remove', nick, division))

    @discord.ui.button(label="+ Устник", style=discord.ButtonStyle.danger, custom_id="staff_punish_strike_add")
    async def strike_add(self, interaction, button):
        nick, division = await _state_or_error(interaction)
        if nick:
            await interaction.response.send_modal(StaffPunishmentModal('strike', 'add', nick, division))

    @discord.ui.button(label="- Устник", style=discord.ButtonStyle.gray, custom_id="staff_punish_strike_rem")
    async def strike_rem(self, interaction, button):
        nick, division = await _state_or_error(interaction)
        if nick:
            await interaction.response.send_modal(StaffPunishmentModal('strike', 'remove', nick, division))

    @discord.ui.button(label="⬅ Назад", style=discord.ButtonStyle.gray, custom_id="staff_punish_back")
    async def back_btn(self, interaction, button):
        nick, division = await _state_or_error(interaction)
        if nick:
            await _return_to_profile(interaction, nick, division)


class StaffAccessView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Выдать доступ", style=discord.ButtonStyle.green, custom_id="staff_access_grant")
    async def grant_btn(self, interaction, button):
        nick, division = await _state_or_error(interaction)
        if nick:
            await interaction.response.send_modal(StaffAccessModal('grant', nick, division))

    @discord.ui.button(label="Забрать доступ", style=discord.ButtonStyle.red, custom_id="staff_access_revoke")
    async def revoke_btn(self, interaction, button):
        nick, division = await _state_or_error(interaction)
        if not nick:
            return
        # Подставляем текущую почту из таблицы в модалку (одна короткая
        # операция). Ошибка таблицы не должна мешать открыть модалку —
        # _safe_reload вернёт None, и поле останется пустым.
        user = await asyncio.to_thread(_safe_reload, nick, division)
        email = (user or {}).get('email', '')
        await interaction.response.send_modal(StaffAccessModal('revoke', nick, division, email))

    @discord.ui.button(label="⬅ Назад", style=discord.ButtonStyle.gray, custom_id="staff_access_back")
    async def back_btn(self, interaction, button):
        nick, division = await _state_or_error(interaction)
        if nick:
            await _return_to_profile(interaction, nick, division)


class StaffMainView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(
        placeholder="Выберите категорию управления...",
        custom_id="staff_main_select",
        options=[
            discord.SelectOption(label="Баллы", value="points",
                                 description="Управление балансом сотрудника", emoji="💰"),
            discord.SelectOption(label="Наказания", value="punish",
                                 description="Варны и устники", emoji="⚠️"),
            discord.SelectOption(label="Доступы", value="access",
                                 description="Google Drive & Forms", emoji="🔑"),
        ],
    )
    async def select_callback(self, interaction, select):
        nick, division = await _state_or_error(interaction)
        if not nick:
            return
        choice = select.values[0]
        await interaction.response.defer()
        try:
            user = await asyncio.to_thread(_reload, nick, division)
        except Exception as e:
            logger.error(f"Reload failed in select_callback: {e}", exc_info=True)
            user, error_text = None, describe_google_error(e)
        else:
            error_text = f"❌ Сотрудник `{nick}` не найден в таблице {division}."
        if user is None:
            await interaction.edit_original_response(
                embed=BotStyle.create_embed(
                    compose_title(TITLE_PROFILE, nick, division),
                    error_text,
                    color=BotStyle.ERROR_COLOR,
                ),
                view=None,
            )
            return

        if choice == "points":
            embed = BotStyle.create_embed(
                compose_title(TITLE_POINTS, nick, division),
                f"Текущий баланс: **{user['points']}** баллов. Выберите действие:",
            )
            view = StaffPointsView()
        elif choice == "punish":
            embed = BotStyle.create_embed(
                compose_title(TITLE_PUNISH, nick, division),
                f"Варны: `{user['warns']}` | Устники: `{user['warnings']}`\nВыберите действие:",
            )
            view = StaffPunishmentView()
        else:  # access
            embed = BotStyle.create_embed(
                compose_title(TITLE_ACCESS, nick, division),
                f"Выберите действие для управления доступами в дивизионе {division}.",
            )
            view = StaffAccessView()
        await interaction.edit_original_response(embed=embed, view=view)


# Классы persistent views для регистрации в bot.setup_hook.
# Храним КЛАССЫ, а не экземпляры: View.__init__ вызывает
# asyncio.get_running_loop(), поэтому инстанцировать их можно только
# внутри работающего event loop'а (т.е. в setup_hook), не на уровне модуля.
PERSISTENT_VIEWS = [StaffMainView, StaffPointsView, StaffPunishmentView, StaffAccessView]
