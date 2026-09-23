import discord
from discord import app_commands
from google_manager import google_manager, find_user_by_nick, parse_points
from config import (
    FT_GOOGLE_SHEETS_ID, FT_SHEET_NAME, FT_GOOGLE_FORM_ID,
    RW_GOOGLE_SHEETS_ID, RW_SHEET_NAME, RW_GOOGLE_FORM_ID,
    FT_WARN_COST, FT_WARNING_COST, FT_VACATION_COST,
    RW_WARN_COST, RW_WARNING_COST, RW_VACATION_COST
)
from style import BotStyle
import logging

logger = logging.getLogger('StaffBot.UI')

async def _return_to_main(interaction, employee_data, division):
    user_data = None
    if division == 'FT':
        user_data = find_user_by_nick(employee_data['nick'], FT_GOOGLE_SHEETS_ID, FT_SHEET_NAME, google_manager)
    else:
        user_data = find_user_by_nick(employee_data['nick'], RW_GOOGLE_SHEETS_ID, RW_SHEET_NAME, google_manager)

    if not user_data:
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
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=StaffMainView(user_data, division))
        else:
            await interaction.response.edit_message(embed=embed, view=StaffMainView(user_data, division))
    except Exception as e:
        logger.error(f"Error in _return_to_main: {e}")

class StaffAccessModal(discord.ui.Modal):
    def __init__(self, action_type, employee_data, division):
        if action_type == 'revoke':
            super().__init__(title="Отзыв доступа")
            self.action_type = action_type
            self.employee_data = employee_data
            self.division = division
            self.email = employee_data.get('email', '').strip().lower()
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
        email = self.email if self.action_type == 'revoke' else self.email_input.value.strip().lower()
        import re
        if not email or not re.match(r"[^@]+@[^@]+\.[^@]+", email):
            await interaction.response.send_message("❌ Почта не найдена в таблице или имеет некорректный формат.", ephemeral=True)
            return

        await interaction.response.defer()
        try:
            service = google_manager.get_drive_service()
            sheet_id = FT_GOOGLE_SHEETS_ID if self.division == 'FT' else RW_GOOGLE_SHEETS_ID
            form_id = FT_GOOGLE_FORM_ID if self.division == 'FT' else RW_GOOGLE_FORM_ID

            if self.action_type == 'grant':
                service.permissions().create(fileId=sheet_id, body={'type': 'user', 'role': 'writer', 'emailAddress': email}).execute()
                service.permissions().create(fileId=form_id, body={'type': 'user', 'role': 'writer', 'emailAddress': email}).execute()
                msg = f"Доступ **выдан** для {email}"
                color = BotStyle.SUCCESS_COLOR
            else:
                resources = [('Таблица', sheet_id), ('Форма', form_id)]
                for res_name, res_id in resources:
                    try:
                        perms = service.permissions().list(fileId=res_id, fields="permissions(id, emailAddress)").execute()
                        for p in perms.get('permissions', []):
                            if p.get('emailAddress', '').lower() == email:
                                service.permissions().delete(fileId=res_id, permissionId=p['id']).execute()
                    except Exception as e:
                        logger.error(f"Error revoking {res_name} access ({res_id}): {e}")
                msg = f"Доступ **полностью отозван** для {email}"
                color = BotStyle.WARNING_COLOR

            updated_data = find_user_by_nick(self.employee_data['nick'], 
                                           FT_GOOGLE_SHEETS_ID if self.division == 'FT' else RW_GOOGLE_SHEETS_ID,
                                           FT_SHEET_NAME if self.division == 'FT' else RW_SHEET_NAME, 
                                           google_manager)
            
            fields = [
                ("📧 Почта", updated_data.get('email', 'Не указана'), True),
                ("🍀 Должность", updated_data['position'], True),
                ("💰 Баллы", updated_data['points'], True),
                ("☠ Варны", updated_data['warns'], True),
                ("☠ Устники", updated_data['warnings'], True),
            ]
            embed = BotStyle.create_embed(f"📊 Профиль сотрудника • {updated_data['nick']}", f"Дивизион: **{self.division}**\n\n🔑 {msg}", color=color, fields=fields)
            await interaction.edit_original_response(embed=embed, view=StaffMainView(updated_data, self.division))
            
            # Импортируем log_action локально чтобы избежать циклической зависимости
            from bot_new import log_action
            await log_action(interaction, f"[{self.division}] {self.action_type} access for {email} ({self.employee_data['nick']})")
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка Google API: {e}", ephemeral=True)

class StaffPointsModal(discord.ui.Modal):
    def __init__(self, action_type, employee_data, division):
        super().__init__(title=f"{'Добавление' if action_type == 'add' else 'Списание'} баллов")
        self.action_type = action_type
        self.employee_data = employee_data
        self.division = division
        self.amount_input = discord.ui.TextInput(label="Количество баллов", placeholder="Например: 500", required=True)
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = int(self.amount_input.value)
            if amount <= 0: raise ValueError()
        except ValueError:
            await interaction.response.send_message("❌ Введите корректное положительное число.", ephemeral=True)
            return

        await interaction.response.defer()
        sheet_id = FT_GOOGLE_SHEETS_ID if self.division == 'FT' else RW_GOOGLE_SHEETS_ID
        sheet_name = FT_SHEET_NAME if self.division == 'FT' else RW_SHEET_NAME
        sheet = google_manager.get_sheet(sheet_id, sheet_name)
        
        row = self.employee_data['row']
        current_points = parse_points(self.employee_data['points'])
        new_points = (current_points + amount) if self.action_type == 'add' else max(0, current_points - amount)
        
        try:
            sheet.update_cell(row, 3, str(new_points))
            updated_data = find_user_by_nick(self.employee_data['nick'], sheet_id, sheet_name, google_manager)
            fields = [("🍀 Должность", updated_data['position'], True), ("💰 Баллы", updated_data['points'], True), ("☠ Варны", updated_data['warns'], True), ("☠ Устники", updated_data['warnings'], True)]
            embed = BotStyle.create_embed(f"📊 Профиль сотрудника • {updated_data['nick']}", f"Дивизион: **{self.division}**\n\n💰 {self.employee_data['nick']} было {'выдано' if self.action_type == 'add' else 'списано'} **{amount}** баллов.", color=BotStyle.SUCCESS_COLOR if self.action_type == 'add' else BotStyle.WARNING_COLOR, fields=fields)
            await interaction.edit_original_response(embed=embed, view=StaffMainView(updated_data, self.division))
            
            from bot_new import log_action
            await log_action(interaction, f"[{self.division}] {self.action_type} points ({amount}) to {self.employee_data['nick']}. Total: {new_points}")
        except Exception as e:
            await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)

class StaffPunishmentModal(discord.ui.Modal):
    def __init__(self, punish_type, action_type, employee_data, division):
        super().__init__(title=f"{'Выдача' if action_type == 'add' else 'Снятие'} {punish_type}")
        self.punish_type, self.action_type, self.employee_data, self.division = punish_type, action_type, employee_data, division
        self.amount_input = discord.ui.TextInput(label="Количество", placeholder="1", required=True)
        self.add_item(self.amount_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = int(self.amount_input.value)
            if amount <= 0: raise ValueError()
        except ValueError:
            await interaction.response.send_message("❌ Введите число.", ephemeral=True)
            return

        await interaction.response.defer()
        sheet_id = FT_GOOGLE_SHEETS_ID if self.division == 'FT' else RW_GOOGLE_SHEETS_ID
        sheet_name = FT_SHEET_NAME if self.division == 'FT' else RW_SHEET_NAME
        sheet = google_manager.get_sheet(sheet_id, sheet_name)
        row = self.employee_data['row']
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
            msg, color = f"Выдано {amount} {'варн' if self.punish_type == 'warn' else 'устник'}(ов).", BotStyle.WARNING_COLOR
        else:
            if current_count < amount:
                await interaction.followup.send("❌ Недостаточно наказаний.", ephemeral=True)
                return
            cost = (FT_WARN_COST if self.punish_type == 'warn' else FT_WARNING_COST) if self.division == 'FT' else (RW_WARN_COST if self.punish_type == 'warn' else RW_WARNING_COST)
            current_points = parse_points(self.employee_data['points'])
            if current_points < cost:
                await interaction.followup.send(f"❌ Недостаточно баллов! Требуется: {cost}", ephemeral=True)
                return
            new_count = current_count - amount
            sheet.update_cell(row, col, f"{new_count}/3")
            sheet.update_cell(row, 3, str(current_points - cost))
            msg, color = f"Снято {amount} {'варн' if self.punish_type == 'warn' else 'устник'}(ов). Списано {cost} баллов.", BotStyle.SUCCESS_COLOR

        updated_data = find_user_by_nick(self.employee_data['nick'], sheet_id, sheet_name, google_manager)
        fields = [("🍀 Должность", updated_data['position'], True), ("💰 Баллы", updated_data['points'], True), ("☠ Варны", updated_data['warns'], True), ("☠ Устники", updated_data['warnings'], True)]
        embed = BotStyle.create_embed(f"📊 Профиль сотрудника • {updated_data['nick']}", f"Дивизион: **{self.division}**\n\n⚠️ {msg}", color=color, fields=fields)
        await interaction.edit_original_response(embed=embed, view=StaffMainView(updated_data, self.division))
        
        from bot_new import log_action
        await log_action(interaction, f"[{self.division}] {self.action_type} {self.punish_type} for {self.employee_data['nick']}")

class StaffPointsView(discord.ui.View):
    def __init__(self, employee_data, division):
        super().__init__(timeout=None)
        self.employee_data, self.division = employee_data, division

    @discord.ui.button(label="Добавить баллы", style=discord.ButtonStyle.green, custom_id="points_add")
    async def add_btn(self, interaction, button): await interaction.response.send_modal(StaffPointsModal('add', self.employee_data, self.division))
    @discord.ui.button(label="Списать баллы", style=discord.ButtonStyle.red, custom_id="points_sub")
    async def sub_btn(self, interaction, button): await interaction.response.send_modal(StaffPointsModal('sub', self.employee_data, self.division))
    @discord.ui.button(label="⬅ Назад", style=discord.ButtonStyle.gray, custom_id="points_back")
    async def back_btn(self, interaction, button): await _return_to_main(interaction, self.employee_data, self.division)

class StaffPunishmentView(discord.ui.View):
    def __init__(self, employee_data, division):
        super().__init__(timeout=None)
        self.employee_data, self.division = employee_data, division

    @discord.ui.button(label="+ Варн", style=discord.ButtonStyle.danger, custom_id="punish_warn_add")
    async def warn_add(self, interaction, button): await interaction.response.send_modal(StaffPunishmentModal('warn', 'add', self.employee_data, self.division))
    @discord.ui.button(label="- Варн", style=discord.ButtonStyle.gray, custom_id="punish_warn_rem")
    async def warn_rem(self, interaction, button): await interaction.response.send_modal(StaffPunishmentModal('warn', 'remove', self.employee_data, self.division))
    @discord.ui.button(label="+ Устник", style=discord.ButtonStyle.danger, custom_id="punish_strike_add")
    async def strike_add(self, interaction, button): await interaction.response.send_modal(StaffPunishmentModal('strike', 'add', self.employee_data, self.division))
    @discord.ui.button(label="- Устник", style=discord.ButtonStyle.gray, custom_id="punish_strike_rem")
    async def strike_rem(self, interaction, button): await interaction.response.send_modal(StaffPunishmentModal('strike', 'remove', self.employee_data, self.division))
    @discord.ui.button(label="⬅ Назад", style=discord.ButtonStyle.gray, custom_id="punish_back")
    async def back_btn(self, interaction, button): await _return_to_main(interaction, self.employee_data, self.division)

class StaffAccessView(discord.ui.View):
    def __init__(self, employee_data, division):
        super().__init__(timeout=None)
        self.employee_data, self.division = employee_data, division

    @discord.ui.button(label="Выдать доступ", style=discord.ButtonStyle.green, custom_id="access_grant")
    async def grant_btn(self, interaction, button): await interaction.response.send_modal(StaffAccessModal('grant', self.employee_data, self.division))
    @discord.ui.button(label="Забрать доступ", style=discord.ButtonStyle.red, custom_id="access_revoke")
    async def revoke_btn(self, interaction, button): await interaction.response.send_modal(StaffAccessModal('revoke', self.employee_data, self.division))
    @discord.ui.button(label="⬅ Назад", style=discord.ButtonStyle.gray, custom_id="access_back")
    async def back_btn(self, interaction, button): await _return_to_main(interaction, self.employee_data, self.division)

class StaffMainView(discord.ui.View):
    def __init__(self, employee_data, division):
        super().__init__(timeout=None)
        self.employee_data, self.division = employee_data, division

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
            embed = BotStyle.create_embed(f"🔑 Управление доступами • {self.employee_data['nick']}", f"Выберите действие для управления доступами в дивизионе {self.division}.", color=BotStyle.PRIMARY_COLOR)
            await interaction.response.edit_message(embed=embed, view=StaffAccessView(self.employee_data, self.division))
        elif choice == "💰 Баллы":
            embed = BotStyle.create_embed(f"💰 Управление баллами • {self.employee_data['nick']}", f"Текущий баланс: **{self.employee_data['points']}** баллов. Выберите действие:", color=BotStyle.PRIMARY_COLOR)
            await interaction.response.edit_message(embed=embed, view=StaffPointsView(self.employee_data, self.division))
        elif choice == "⚠️ Наказания":
            embed = BotStyle.create_embed(f"⚠️ Управление наказаниями • {self.employee_data['nick']}", f"Варны: `{self.employee_data['warns']}` | Устники: `{self.employee_data['warnings']}`\nВыберите действие:", color=BotStyle.PRIMARY_COLOR)
            await interaction.response.edit_message(embed=embed, view=StaffPunishmentView(self.employee_data, self.division))
