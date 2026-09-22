import os
from dotenv import load_dotenv

load_dotenv()

# ===== DISCORD =====
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')

# ===== НАСТРОЙКИ FT (artytest-main) =====
FT_GOOGLE_SHEETS_ID = os.getenv('FT_GOOGLE_SHEETS_ID', '1-3ER99-RpUkPdeRE4JC5s0KRnV1unqNnmNQhtf4J7a4')
FT_SHEET_NAME = os.getenv('FT_SHEET_NAME', 'Состав FT')
FT_GOOGLE_FORM_ID = os.getenv('FT_GOOGLE_FORM_ID', '1NZmtXX4kHrOH8U1n6QQ2fAZbeikAoRZ1xwMjRtv_yMc')

# Цены FT
FT_WARN_COST = int(os.getenv('FT_WARN_COST', 500))
FT_WARNING_COST = int(os.getenv('FT_WARNING_COST', 175))
FT_VACATION_COST = int(os.getenv('FT_VACATION_COST', 100))

# Строки заголовков для дат в FT
FT_HEADER_ROW_FIRST = int(os.getenv('FT_HEADER_ROW_FIRST', 15))
FT_HEADER_ROW_SECOND = int(os.getenv('FT_HEADER_ROW_SECOND', 60))

# Источник копирования для отпуска FT (строка 80 = индекс 79)
FT_VACATION_SOURCE_ROW = int(os.getenv('FT_VACATION_SOURCE_ROW', 79))

# ===== НАСТРОЙКИ RW (rwtest-main) =====
RW_GOOGLE_SHEETS_ID = os.getenv('RW_GOOGLE_SHEETS_ID', '1-3ER99-RpUkPdeRE4JC5s0KRnV1unqNnmNQhtf4J7a4')
RW_SHEET_NAME = os.getenv('RW_SHEET_NAME', 'Состав RW')
RW_GOOGLE_FORM_ID = os.getenv('RW_GOOGLE_FORM_ID', '1NZmtXX4kHrOH8U1n6QQ2fAZbeikAoRZ1xwMjRtv_yMc')

# Цены RW
RW_WARN_COST = int(os.getenv('RW_WARN_COST', 1000))
RW_WARNING_COST = int(os.getenv('RW_WARNING_COST', 350))
RW_VACATION_COST = int(os.getenv('RW_VACATION_COST', 200))

# Строки заголовков для дат в RW
RW_HEADER_ROW_FIRST = int(os.getenv('RW_HEADER_ROW_FIRST', 20))
RW_HEADER_ROW_SECOND = int(os.getenv('RW_HEADER_ROW_SECOND', 38))

# Источник копирования для отпуска RW (строка 49 = индекс 48)
RW_VACATION_SOURCE_ROW = int(os.getenv('RW_VACATION_SOURCE_ROW', 48))

# ===== НАСТРОЙКА РОЛЕЙ =====
ROLE_OBSZV = int(os.getenv('ROLE_OBSZV', 1546866437233053856))
ROLE_MANAGER = int(os.getenv('ROLE_MANAGER', 1546866019564519574))
ROLE_BOT_MANAGER = int(os.getenv('ROLE_BOT_MANAGER', 1546866078796349460))

# Список всех разрешённых ролей
ALLOWED_ROLES = [ROLE_OBSZV, ROLE_MANAGER, ROLE_BOT_MANAGER]
