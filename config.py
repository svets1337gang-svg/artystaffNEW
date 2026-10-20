import os
from dotenv import load_dotenv

load_dotenv()

# ===== DISCORD =====
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')

# Необязательный ID сервера. Если задан, слэш-команды синкаются в него
# (мгновенно); иначе — глобально (Discord расходится до часа).
DISCORD_GUILD_ID = os.getenv('DISCORD_GUILD_ID')

# ===== НАСТРОЙКИ FT (artytest-main) =====
FT_GOOGLE_SHEETS_ID = os.getenv('FT_GOOGLE_SHEETS_ID', '1-3ER99-RpUkPdeRE4JC5s0KRnV1unqNnmNQhtf4J7a4')
FT_SHEET_NAME = os.getenv('FT_SHEET_NAME', 'Состав FT')
FT_GOOGLE_FORM_ID = os.getenv('FT_GOOGLE_FORM_ID', '1NZmtXX4kHrOH8U1n6QQ2fAZbeikAoRZ1xwMjRtv_yMc')

# Цены FT
FT_WARN_COST = int(os.getenv('FT_WARN_COST', 500))
FT_WARNING_COST = int(os.getenv('FT_WARNING_COST', 175))

# ===== НАСТРОЙКИ RW (rwtest-main) =====
RW_GOOGLE_SHEETS_ID = os.getenv('RW_GOOGLE_SHEETS_ID', '1-3ER99-RpUkPdeRE4JC5s0KRnV1unqNnmNQhtf4J7a4')
RW_SHEET_NAME = os.getenv('RW_SHEET_NAME', 'Состав RW')
RW_GOOGLE_FORM_ID = os.getenv('RW_GOOGLE_FORM_ID', '1NZmtXX4kHrOH8U1n6QQ2fAZbeikAoRZ1xwMjRtv_yMc')

# Цены RW
RW_WARN_COST = int(os.getenv('RW_WARN_COST', 1000))
RW_WARNING_COST = int(os.getenv('RW_WARNING_COST', 350))

# ===== ГРАНИЦЫ ПАРСИНГА СОСТАВА =====
# Строка, с которой начинаются строки сотрудников. Всё выше — шапка листа,
# заголовки секций и служебные строки — не парсятся нигде: ни в /top и /stat,
# ни в автокомплите, ни в аудите, ни в поиске сотрудника.
# FT: с 15-й строки включительно. RW: 17-я не включительно, т.е. с 18-й.
FT_SHEET_START_ROW = int(os.getenv('FT_SHEET_START_ROW', 15))
RW_SHEET_START_ROW = int(os.getenv('RW_SHEET_START_ROW', 18))

# ===== ПОТОЛКИ НАКАЗАНИЙ =====
# WARN_LIMIT — максимум варнов; STRIKE_LIMIT — сколько устников
# автоматически конвертируются в +1 варн. В таблицу значение пишется
# как «count/потолок»; при достижении потолка варнов ячейка помечается
# «(MAX)», а в лог-канал уходит отдельный алерт.
FT_WARN_LIMIT = int(os.getenv('FT_WARN_LIMIT', 3))
FT_STRIKE_LIMIT = int(os.getenv('FT_STRIKE_LIMIT', 3))
RW_WARN_LIMIT = int(os.getenv('RW_WARN_LIMIT', 3))
RW_STRIKE_LIMIT = int(os.getenv('RW_STRIKE_LIMIT', 3))