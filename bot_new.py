import discord
from discord import app_commands
from discord.ext import commands
import re
import asyncio
import os
import logging
from datetime import datetime
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import gspread

from config import (
    DISCORD_TOKEN,
    FT_GOOGLE_SHEETS_ID, FT_SHEET_NAME, FT_GOOGLE_FORM_ID,
    FT_WARN_COST, FT_WARNING_COST, FT_VACATION_COST,
    RW_GOOGLE_SHEETS_ID, RW_SHEET_NAME, RW_GOOGLE_FORM_ID,
    RW_WARN_COST, RW_WARNING_COST, RW_VACATION_COST,
    ALLOWED_ROLES
)
from ui_components import (
    StaffMainView, StaffAccessView, StaffPointsView, StaffPunishmentView,
    StaffAccessModal, StaffPointsModal, StaffPunishmentModal
)
from style import BotStyle
from google_manager import google_manager, normalize_nick, parse_points

# ===== НАСТРОЙКИ ЛОГИРОВАНИЯ =====
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('StaffBot')
# =========================================================
# UI COMPONENTS (Interactive Layer)
# =========================================================

async def auto_delete_message(message, delay=420):
    """Удаляет сообщение через указанное количество секунд (по умолчанию 7 минут)"""
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"Auto-delete failed: {e}")

# Код интерфейса (Modals, Views) перенесен в ui_components.py
class StaffAccessModal(discord.ui.Modal):
    def __init__(self, action_type, employee_data, division):
        # Если мы забираем доступ, почта берется из таблицы, и ввод не нужен
        if action_type == 'revoke':
            super().__init__(title="Отзыв доступа")
            self.action_type = action_type
            self.employee_data = employee_data
            self.division = division
            self.email = employee_data.get('email', '').strip().lower()
            # Модалка всё равно нужна для вызова, но мы её не будем использовать для ввода
            # Просто добавим информационное поле без read_only (так как discord.py его не поддерживает)
            self.info_input = discord.ui.TextInput(
                label="Почта сотрудника",
                default=self.email if self.email else "Не указана",
                required=False
            )
            self.add_item(self.info_input)
        else:
            super().__init__(title="Выдача доступа")
            self.action_type = action_type
            self.employee_data = employee_data
            self.division = division
            self.email_input = discord.ui.TextInput(
                label="Email пользователя",
                placeholder="example@gmail.com",
                required=True,
                min_length=5,
                max_length=100
            )
            self.add_item(self.email_input)

    async def on_submit(self, interaction: discord.Interaction):
        if self.action_type == 'revoke':
            email = self.email
        else:
            email = self.email_input.value.strip().lower()

        if not email or not re.match(r"[^@]+@[^@]+\.[^@]+", email):
            await interaction.response.send_message("❌ Почта не найдена в таблице или имеет некорректный формат.", ephemeral=True)
            return

        await interaction.response.defer()

        try:
            service = google_manager.get_drive_service()
            # ID ТАБЛИЦЫ
            sheet_id = FT_GOOGLE_SHEETS_ID if self.division == 'FT' else RW_GOOGLE_SHEETS_ID
            # ID ФОРМЫ
            form_id = FT_GOOGLE_FORM_ID if self.division == 'FT' else RW_GOOGLE_FORM_ID

            if self.action_type == 'grant':
                # Выдача доступа к таблице
                service.permissions().create(
                    fileId=sheet_id,
                    body={'type': 'user', 'role': 'writer', 'emailAddress': email}
                ).execute()
                # Выдача доступа к форме
                service.permissions().create(
                    fileId=form_id,
                    body={'type': 'user', 'role': 'writer', 'emailAddress': email}
                ).execute()
                msg = f"Доступ **выдан** для {email}"
                color = BotStyle.SUCCESS_COLOR
            else:
                # Отзыв доступа из всех ресурсов дивизиона
                resources = [
                    ('Таблица', sheet_id),
                    ('Форма', form_id)
                ]
                
                revoked_count = 0
                for res_name, res_id in resources:
                    try:
                        perms = service.permissions().list(fileId=res_id, fields="permissions(id, emailAddress)").execute()
                        for p in perms.get('permissions', []):
                            if p.get('emailAddress', '').lower() == email:
                                service.permissions().delete(fileId=res_id, permissionId=p['id']).execute()
                                revoked_count += 1
                    except Exception as e:
                        logger.error(f"Error revoking {res_name} access ({res_id}): {e}")

                msg = f"Доступ **полностью отозван** для {email} (обработано ресурсов: {len(resources)})"
                color = BotStyle.WARNING_COLOR

            # Обновляем профиль
            updated_data = None
            if self.division == 'FT':
                updated_data = find_user_by_nick(self.employee_data['nick'], FT_GOOGLE_SHEETS_ID, FT_SHEET_NAME)
            else:
                updated_data = find_user_by_nick(self.employee_data['nick'], RW_GOOGLE_SHEETS_ID, RW_SHEET_NAME)

            fields = [
                ("📧 Почта", updated_data.get('email', 'Не указана'), True),
                ("🍀 Должность", updated_data['position'], True),
                ("💰 Баллы", updated_data['points'], True),
                ("☠ Варны", updated_data['warns'], True),
                ("☠ Устники", updated_data['warnings'], True),
            ]
            embed = BotStyle.create_embed(
                f"📊 Профиль сотрудника • {updated_data['nick']}",
                f"Дивизион: **{self.division}**\n\n🔑 {msg}",
                color=color,
                fields=fields
            )
            await interaction.edit_original_response(embed=embed, view=StaffMainView(updated_data, self.division))
            await log_action(interaction, f"[{self.division}] {self.action_type} access for {email} ({self.employee_data['nick']})")

        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка Google API: {e}", ephemeral=True)
async def _return_to_main(interaction: discord.Interaction, employee_data, division):
    # Обновляем данные из таблицы перед возвратом, чтобы статы были актуальными
    user_data = None
    if division == 'FT':
        user_data = find_user_by_nick(employee_data['nick'], FT_GOOGLE_SHEETS_ID, FT_SHEET_NAME)
    else:
        user_data = find_user_by_nick(employee_data['nick'], RW_GOOGLE_SHEETS_ID, RW_SHEET_NAME)

    if not user_data:
        # Если данных нет, используем старые из employee_data, чтобы не вылетать с ошибкой
        user_data = employee_data

    fields = [
        ("📧 Почта", user_data.get('email', 'Не указана'), True),
        ("🍀 Должность", user_data['position'], True),
        ("💰 Баллы", user_data['points'], True),
        ("☠ Варны", user_data['warns'], True),
        ("☠ Устники", user_data['warnings'], True),
    ]
    embed = BotStyle.create_embed(
        f"📊 Профиль сотрудника • {user_data['nick']}",
        f"Дивизион: **{division}**",
        fields=fields
    )

    try:
        # ПРОВЕРЯЕМ, ОТВЕЧЕНО ЛИ УЖЕ НА ВЗАИМОДЕЙСТВИЕ
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=StaffMainView(user_data, division))
        else:
            await interaction.response.edit_message(embed=embed, view=StaffMainView(user_data, division))
    except Exception as e:
        logger.error(f"Error in _return_to_main: {e}")
        # Попытка через followup как крайний вариант
        try:
            await interaction.followup.edit_message(embed=embed, view=StaffMainView(user_data, division))
        except:
            pass
class StaffPointsModal(discord.ui.Modal):
    def __init__(self, action_type, employee_data, division):
        super().__init__(title=f"{'Добавление' if action_type == 'add' else 'Списание'} баллов")
        self.action_type = action_type
        self.employee_data = employee_data
        self.division = division

        self.amount_input = discord.ui.TextInput(
            label="Количество баллов",
            placeholder="Например: 500",
            required=True,
            min_length=1,
            max_length=10
        )
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = int(self.amount_input.value)
            if amount <= 0:
                raise ValueError("Число должно быть положительным")
        except ValueError:
            await interaction.response.send_message("❌ Пожалуйста, введите корректное целое положительное число.", ephemeral=True)
            return

        await interaction.response.defer()

        sheet_id = FT_GOOGLE_SHEETS_ID if self.division == 'FT' else RW_GOOGLE_SHEETS_ID
        sheet_name = FT_SHEET_NAME if self.division == 'FT' else RW_SHEET_NAME

        sheet = google_manager.get_sheet(sheet_id, sheet_name)
        if not sheet:
            await interaction.followup.send("❌ Ошибка подключения к таблице.", ephemeral=True)
            return

        row = self.employee_data['row']
        current_points = parse_points(self.employee_data['points'])

        if self.action_type == 'add':
            new_points = current_points + amount
            action_msg = "выдано"
            color = BotStyle.SUCCESS_COLOR
        else:
            new_points = max(0, current_points - amount)
            action_msg = "списано"
            color = BotStyle.WARNING_COLOR

        try:
            sheet.update_cell(row, 3, str(new_points))

            # Обновляем данные сотрудника для отображения в новом профиле
            updated_data = None
            if self.division == 'FT':
                updated_data = find_user_by_nick(self.employee_data['nick'], FT_GOOGLE_SHEETS_ID, FT_SHEET_NAME)
            else:
                updated_data = find_user_by_nick(self.employee_data['nick'], RW_GOOGLE_SHEETS_ID, RW_SHEET_NAME)

            fields = [
                ("🍀 Должность", updated_data['position'], True),
                ("💰 Баллы", updated_data['points'], True),
                ("☠ Варны", updated_data['warns'], True),
                ("☠ Устники", updated_data['warnings'], True),
            ]
            embed = BotStyle.create_embed(
                f"📊 Профиль сотрудника • {updated_data['nick']}",
                f"Дивизион: **{self.division}**\n\n💰 {self.employee_data['nick']} было {action_msg} **{amount}** баллов.\nИтог: `{current_points}` → `{new_points}`",
                color=color,
                fields=fields
            )
            # ИСПОЛЬЗУЕМ edit_original_response вместо followup.edit_message
            await interaction.edit_original_response(embed=embed, view=StaffMainView(updated_data, self.division))
            await log_action(interaction, f"[{self.division}] {self.action_type} points ({amount}) to {self.employee_data['nick']}. Total: {new_points}")
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка при обновлении таблицы: {e}", ephemeral=True)

class StaffPunishmentModal(discord.ui.Modal):
    def __init__(self, punish_type, action_type, employee_data, division):
        super().__init__(title=f"{'Выдача' if action_type == 'add' else 'Снятие'} {punish_type}")
        self.punish_type = punish_type
        self.action_type = action_type
        self.employee_data = employee_data
        self.division = division

        self.amount_input = discord.ui.TextInput(
            label="Количество",
            placeholder="1",
            required=True,
            min_length=1,
            max_length=2
        )
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = int(self.amount_input.value)
            if amount <= 0: raise ValueError()
        except ValueError:
            await interaction.response.send_message("❌ Введите число от 1 до 3.", ephemeral=True)
            return

        await interaction.response.defer()

        sheet_id = FT_GOOGLE_SHEETS_ID if self.division == 'FT' else RW_GOOGLE_SHEETS_ID
        sheet_name = FT_SHEET_NAME if self.division == 'FT' else RW_SHEET_NAME
        sheet = google_manager.get_sheet(sheet_id, sheet_name)

        row = self.employee_data['row']
        # Индексы: 4 - варны, 5 - устники
        col = 4 if self.punish_type == 'warn' else 5

        current_val_str = self.employee_data['warns'] if self.punish_type == 'warn' else self.employee_data['warnings']
        current_count = int(current_val_str.split('/')[0]) if current_val_str else 0

        if self.action_type == 'add':
            new_count = current_count + amount
            if self.punish_type == 'strike':
                extra_warns = new_count // 3
                new_count = new_count % 3
                if extra_warns > 0:
                    warn_val = sheet.cell(row, 4).value or "0/3"
                    warn_count = int(warn_val.split('/')[0])
                    sheet.update_cell(row, 4, f"{warn_count + extra_warns}/3")

            sheet.update_cell(row, col, f"{new_count}/3")
            msg = f"Выдано {amount} {'варн' if self.punish_type == 'warn' else 'устник'}(ов)."
            color = BotStyle.WARNING_COLOR
        else:
            if current_count < amount:
                await interaction.followup.send("❌ Недостаточно наказаний для снятия.", ephemeral=True)
                return

            cost = 0
            if self.division == 'FT':
                cost = FT_WARN_COST if self.punish_type == 'warn' else FT_WARNING_COST
            else:
                cost = RW_WARN_COST if self.punish_type == 'warn' else RW_WARNING_COST

            current_points = parse_points(self.employee_data['points'])
            if current_points < cost:
                await interaction.followup.send(f"❌ Недостаточно баллов! Требуется: {cost}", ephemeral=True)
                return

            new_count = current_count - amount
            sheet.update_cell(row, col, f"{new_count}/3")
            sheet.update_cell(row, 3, str(current_points - cost))

            msg = f"Снято {amount} {'варн' if self.punish_type == 'warn' else 'устник'}(ов). Списано {cost} баллов."
            color = BotStyle.SUCCESS_COLOR

        # Обновляем профиль вместо отправки нового сообщения
        updated_data = None
        if self.division == 'FT':
            updated_data = find_user_by_nick(self.employee_data['nick'], FT_GOOGLE_SHEETS_ID, FT_SHEET_NAME)
        else:
            updated_data = find_user_by_nick(self.employee_data['nick'], RW_GOOGLE_SHEETS_ID, RW_SHEET_NAME)

        fields = [
            ("🍀 Должность", updated_data['position'], True),
            ("💰 Баллы", updated_data['points'], True),
            ("☠ Варны", updated_data['warns'], True),
            ("☠ Устники", updated_data['warnings'], True),
        ]
        embed = BotStyle.create_embed(
            f"📊 Профиль сотрудника • {updated_data['nick']}",
            f"Дивизион: **{self.division}**\n\n⚠️ {msg}\nИтог: `{current_val_str}` → `{new_count}/3`",
            color=color,
            fields=fields
        )
        await interaction.edit_original_response(embed=embed, view=StaffMainView(updated_data, self.division))
        await log_action(interaction, f"[{self.division}] {self.action_type} {self.punish_type} for {self.employee_data['nick']}")

class StaffPointsView(discord.ui.View):
    def __init__(self, employee_data, division):
        super().__init__(timeout=None)
        self.employee_data = employee_data
        self.division = division

    @discord.ui.button(label="Добавить баллы", style=discord.ButtonStyle.green, custom_id="points_add")
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(StaffPointsModal('add', self.employee_data, self.division))

    @discord.ui.button(label="Списать баллы", style=discord.ButtonStyle.red, custom_id="points_sub")
    async def sub_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(StaffPointsModal('sub', self.employee_data, self.division))

    @discord.ui.button(label="⬅ Назад", style=discord.ButtonStyle.gray, custom_id="points_back")
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _return_to_main(interaction, self.employee_data, self.division)

class StaffPunishmentView(discord.ui.View):
    def __init__(self, employee_data, division):
        super().__init__(timeout=None)
        self.employee_data = employee_data
        self.division = division

    @discord.ui.button(label="+ Варн", style=discord.ButtonStyle.danger, custom_id="punish_warn_add")
    async def warn_add(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(StaffPunishmentModal('warn', 'add', self.employee_data, self.division))

    @discord.ui.button(label="- Варн", style=discord.ButtonStyle.gray, custom_id="punish_warn_rem")
    async def warn_rem(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(StaffPunishmentModal('warn', 'remove', self.employee_data, self.division))

    @discord.ui.button(label="+ Устник", style=discord.ButtonStyle.danger, custom_id="punish_strike_add")
    async def strike_add(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(StaffPunishmentModal('strike', 'add', self.employee_data, self.division))

    @discord.ui.button(label="- Устник", style=discord.ButtonStyle.gray, custom_id="punish_strike_rem")
    async def strike_rem(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(StaffPunishmentModal('strike', 'remove', self.employee_data, self.division))

    @discord.ui.button(label="⬅ Назад", style=discord.ButtonStyle.gray, custom_id="punish_back")
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _return_to_main(interaction, self.employee_data, self.division)

class StaffAccessView(discord.ui.View):
    def __init__(self, employee_data, division):
        super().__init__(timeout=None)
        self.employee_data = employee_data
        self.division = division

    @discord.ui.button(label="Выдать доступ", style=discord.ButtonStyle.green, custom_id="access_grant")
    async def grant_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(StaffAccessModal('grant', self.employee_data, self.division))

    @discord.ui.button(label="Забрать доступ", style=discord.ButtonStyle.red, custom_id="access_revoke")
    async def revoke_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(StaffAccessModal('revoke', self.employee_data, self.division))

    @discord.ui.button(label="⬅ Назад", style=discord.ButtonStyle.gray, custom_id="access_back")
    async def back_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _return_to_main(interaction, self.employee_data, self.division)

class StaffMainView(discord.ui.View):
    def __init__(self, employee_data, division):
        super().__init__(timeout=None)
        self.employee_data = employee_data
        self.division = division

    @discord.ui.select(
        placeholder="Выберите категорию управления...",
        options=[
            discord.SelectOption(label="💰 Баллы", description="Управление балансом сотрудника", emoji="💰"),
            discord.SelectOption(label="⚠️ Наказания", description="Варны и устники", emoji="⚠️"),
            discord.SelectOption(label="🔑 Доступы", description="Google Drive & Forms", emoji="🔑"),
        ]
    )
    async def select_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        choice = select.values[0]
        
        if choice == "🔑 Доступы":
            embed = BotStyle.create_embed(
                f"🔑 Управление доступами • {self.employee_data['nick']}",
                f"Выберите действие для управления доступами в дивизионе {self.division}.",
                color=BotStyle.PRIMARY_COLOR
            )
            await interaction.response.edit_message(embed=embed, view=StaffAccessView(self.employee_data, self.division))
        
        elif choice == "💰 Баллы":
            embed = BotStyle.create_embed(
                f"💰 Управление баллами • {self.employee_data['nick']}",
                f"Текущий баланс: **{self.employee_data['points']}** баллов. Выберите действие:",
                color=BotStyle.PRIMARY_COLOR
            )
            await interaction.response.edit_message(embed=embed, view=StaffPointsView(self.employee_data, self.division))
            
        elif choice == "⚠️ Наказания":
            embed = BotStyle.create_embed(
                f"⚠️ Управление наказаниями • {self.employee_data['nick']}",
                f"Варны: `{self.employee_data['warns']}` | Устники: `{self.employee_data['warnings']}`\nВыберите действие:",
                color=BotStyle.PRIMARY_COLOR
            )
            await interaction.response.edit_message(embed=embed, view=StaffPunishmentView(self.employee_data, self.division))

# =========================================================
# ЯДРО БОТА
# =========================================================
class StaffBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True 
        super().__init__(command_prefix='/', intents=intents)

    async def setup_hook(self):
        # Регистрация постоянных View для работы после перезагрузки бота
        # В реальном проекте здесь добавляются persistent views
        await self.tree.sync()
        logger.info("Commands synced successfully.")

bot = StaffBot()

# ===== ЛОГИРОВАНИЕ ДЕЙСТВИЙ =====
async def log_action(interaction: discord.Interaction, action_text: str):
    LOG_CHANNEL_ID = os.getenv('LOG_CHANNEL_ID')
    if not LOG_CHANNEL_ID:
        return
    try:
        channel = bot.get_channel(int(LOG_CHANNEL_ID))
        if channel:
            embed = BotStyle.create_embed(
                "📝 Лог действий",
                f"**Пользователь:** {interaction.user.mention}\n**Действие:** {action_text}",
                color=discord.Color.light_grey()
            )
            await channel.send(embed=embed)
    except Exception as e:
        logger.error(f"Logging error: {e}")
# ===== АВТОМАТИЗАЦИЯ БЕЗОПАСНОСТИ =====

@bot.event
async def on_member_remove(member):
    """Автоматический отзыв доступов при выходе сотрудника с сервера"""
    logger.info(f"Member {member.name} left the server. Checking for staff access...")
    
    # Ищем сотрудника в обеих таблицах по нику (или ID, если добавим колонку)
    # Для текущей версии ищем по имени пользователя Discord
    nick = member.name
    
    user_data_ft = find_user_by_nick(nick, FT_GOOGLE_SHEETS_ID, FT_SHEET_NAME)
    user_data_rw = find_user_by_nick(nick, RW_GOOGLE_SHEETS_ID, RW_SHEET_NAME)
    
    actions = []
    
    for data, division in [(user_data_ft, 'FT'), (user_data_rw, 'RW')]:
        if data and data['email']:
            email = data['email'].strip().lower()
            try:
                service = google_manager.get_drive_service()
                # Отзываем доступ к таблице
                sheet_id = FT_GOOGLE_SHEETS_ID if division == 'FT' else RW_GOOGLE_SHEETS_ID
                
                # Поиск permissionId
                perms = service.permissions().list(fileId=sheet_id, fields="permissions(id, emailAddress)").execute()
                for p in perms.get('permissions', []):
                    if p.get('emailAddress', '').lower() == email:
                        service.permissions().delete(fileId=sheet_id, permissionId=p['id']).execute()
                
                actions.append(f"Отзван доступ {division} для {email}")
            except Exception as e:
                logger.error(f"Auto-revoke error for {nick}: {e}")

    if actions:
        # Отправляем лог в канал (создаем фиктивный interaction для log_action)
        class MockInteraction:
            def __init__(self): self.user = discord.User(id=bot.user.id)
        
        await log_action(MockInteraction(), f"🚨 **Авто-очистка:** Пользователь {member.mention} покинул сервер. {', '.join(actions)}")

# ===== COMMANDS =====

class AuditView(discord.ui.View):
    def __init__(self, interaction, division='FT'):
        super().__init__(timeout=None)
        self.interaction = interaction
        self.division = division

    async def update_audit(self, interaction, division):
        await interaction.response.defer()
        
        # Параметры в зависимости от дивизиона
        if division == 'FT':
            s_id, s_name = FT_GOOGLE_SHEETS_ID, FT_SHEET_NAME
        else:
            s_id, s_name = RW_GOOGLE_SHEETS_ID, RW_SHEET_NAME

        sheet = google_manager.get_sheet(s_id, s_name)
        if not sheet:
            await interaction.followup.send("❌ Ошибка подключения к таблице.", ephemeral=True)
            return

        names = sheet.col_values(1)
        guild = interaction.guild
        unmatched = []

        for name in names:
            clean_name = normalize_nick(name)
            # ОЧИСТКА: Пропускаем пустые строки, "вакантно" и подобные технические записи
            if not clean_name or clean_name.lower() in ['вакантно', 'vacant', 'пусто']:
                continue

            member = discord.utils.get(guild.members, name=clean_name)
            if not member:
                member = discord.utils.get(guild.members, display_name=clean_name)
            
            if not member:
                unmatched.append(f"• {clean_name}")

        if not unmatched:
            description = f"✅ Все сотрудники дивизиона **{division}** присутствуют на сервере."
            color = BotStyle.SUCCESS_COLOR
        else:
            description = f"Следующие сотрудники **{division}** значатся в таблице, но **отсутствуют** на сервере:\n\n" + "\n".join(unmatched)
            color = BotStyle.WARNING_COLOR

        embed = BotStyle.create_embed(
            f"🔍 Аудит доступов • {division}",
            description,
            color=color
        )
        
        await interaction.edit_original_response(embed=embed, view=AuditView(interaction, division))

    @discord.ui.button(label="Дивизион FT", style=discord.ButtonStyle.primary, custom_id="audit_ft")
    async def ft_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.update_audit(interaction, 'FT')

    @discord.ui.button(label="Дивизион RW", style=discord.ButtonStyle.primary, custom_id="audit_rw")
    async def rw_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.update_audit(interaction, 'RW')

@bot.tree.command(name="audit_access", description="Проверка всех доступов (кто в таблице, но не на сервере)")
async def audit_access(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    
    # По умолчанию запускаем аудит для FT
    view = AuditView(interaction, 'FT')
    # Чтобы первая загрузка прошла, вызываем внутренний метод вручную через фиктивный interaction
    # Но проще всего создать временный класс для первой отправки:
    
    s_id, s_name = FT_GOOGLE_SHEETS_ID, FT_SHEET_NAME
    sheet = google_manager.get_sheet(s_id, s_name)
    if not sheet:
        await interaction.followup.send("❌ Ошибка подключения к таблице.", ephemeral=True)
        return

    names = sheet.col_values(1)
    guild = interaction.guild
    unmatched = []

    for name in names:
        clean_name = normalize_nick(name)
        if not clean_name or clean_name.lower() in ['вакантно', 'vacant', 'пусто']:
            continue
        member = discord.utils.get(guild.members, name=clean_name)
        if not member:
            member = discord.utils.get(guild.members, display_name=clean_name)
        if not member:
            unmatched.append(f"• {clean_name}")

    if not unmatched:
        description = "✅ Все сотрудники дивизиона **FT** присутствуют на сервере."
        color = BotStyle.SUCCESS_COLOR
    else:
        description = "Следующие сотрудники **FT** значатся в таблице, но **отсутствуют** на сервере:\n\n" + "\n".join(unmatched)
        color = BotStyle.WARNING_COLOR

    embed = BotStyle.create_embed(
        "🔍 Аудит доступов • FT",
        description,
        color=color
    )
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)

@bot.tree.command(name="staff", description="Центр управления сотрудником")
@app_commands.describe(nick="Ник сотрудника")
async def staff_command(interaction: discord.Interaction, nick: str):
    # МГНОВЕННО отвечаем Дискорду, чтобы избежать ошибки "Unknown interaction"
    # Ставим ephemeral=False, чтобы сообщение было ВИДНО ВСЕМ
    try:
        await interaction.response.defer(ephemeral=False)
    except discord.errors.NotFound:
        return

    # Поиск пользователя в таблицах
    user_data = None
    division = 'FT'

    # Сначала ищем в FT
    user_data = find_user_by_nick(nick, FT_GOOGLE_SHEETS_ID, FT_SHEET_NAME)
    if not user_data:
        # Если не нашли, ищем в RW
        division = 'RW'
        user_data = find_user_by_nick(nick, RW_GOOGLE_SHEETS_ID, RW_SHEET_NAME)

    if not user_data:
        await interaction.followup.send(f"❌ Сотрудник `{nick}` не найден ни в одном из дивизионов.", ephemeral=False)
        return

    # Создаем красивую карточку статистики
    fields = [
        ("📧 Почта", user_data.get('email', 'Не указана'), True),
        ("🍀 Должность", user_data['position'], True),
        ("💰 Баллы", user_data['points'], True),
        ("☠ Варны", user_data['warns'], True),
        ("☠ Устники", user_data['warnings'], True),
    ]

    embed = BotStyle.create_embed(
        f"📊 Профиль сотрудника • {user_data['nick']}",
        f"Дивизион: **{division}**",
        fields=fields
    )

    await interaction.followup.send(embed=embed, view=StaffMainView(user_data, division), ephemeral=False)
def find_user_by_nick(nick, sheet_id, sheet_name):
    sheet = google_manager.get_sheet(sheet_id, sheet_name)
    if not sheet: return None
    try:
        clean_nick = normalize_nick(nick)
        values = sheet.col_values(1)
        for row_num, value in enumerate(values, start=1):
            if normalize_nick(value) == clean_nick:
                row = sheet.row_values(row_num)
                return {
                    'row': row_num,
                    'nick': row[0] if len(row) > 0 else '',
                    'position': row[1] if len(row) > 1 else '',
                    'points': row[2] if len(row) > 2 else '0',
                    'warns': row[3] if len(row) > 3 else '0/3',
                    'warnings': row[4] if len(row) > 4 else '0/3',
                    'email': row[5] if len(row) > 5 else '',
                }
    except Exception as e:
        logger.error(f"Search error: {e}")
    return None

@bot.event
async def on_ready():
    print(f'🚀 Bot is online: {bot.user}')

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
