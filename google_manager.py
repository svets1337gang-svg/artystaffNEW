import asyncio
import contextlib
import json
import os
import logging
import re
import threading
import time
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import gspread
from gspread import exceptions as gspread_exceptions
from gspread.utils import rowcol_to_a1

from config import (
    FT_GOOGLE_SHEETS_ID, FT_SHEET_NAME, FT_GOOGLE_FORM_ID,
    RW_GOOGLE_SHEETS_ID, RW_SHEET_NAME, RW_GOOGLE_FORM_ID,
    FT_WARN_LIMIT, FT_STRIKE_LIMIT, RW_WARN_LIMIT, RW_STRIKE_LIMIT,
    FT_SHEET_START_ROW, RW_SHEET_START_ROW,
)

logger = logging.getLogger('StaffBot.GoogleManager')

# NoCredentialsError появился только в gspread 6.1 — в 6.0.0 (закреплён в
# requirements.txt) его нет. getattr с фолбэком держит код совместимым с обеими.
_GSPREAD_NO_CREDS = getattr(gspread_exceptions, 'NoCredentialsError', None)


class GoogleAPIError(RuntimeError):
    """Ошибка Google API с уже человекочитаемым текстом.

    Поднимается там, где раньше возвращался None или голый RuntimeError:
    вызывающий код показывает str(e) менеджеру напрямую.
    """


def _http_status(exc):
    """Достаёт HTTP-код из разных обёрток ошибок Google (или None)."""
    resp = getattr(exc, 'resp', None)          # googleapiclient.errors.HttpError
    if resp is not None:
        return getattr(resp, 'status', None)
    resp = getattr(exc, 'response', None)      # gspread.exceptions.APIError
    if resp is not None:
        return getattr(resp, 'status_code', None) or getattr(resp, 'status', None)
    return getattr(exc, 'code', None)


def describe_google_error(exc, sheet_name=None) -> str:
    """Превращает исключение Google API в понятный менеджеру текст.

    Различает «нет прав», «файл не найден», «квота» и «битые креды» —
    универсальное «ошибка при работе с таблицей» не говорит менеджеру,
    что делать, а 403 и 404 требуют противоположных действий.
    """
    if isinstance(exc, GoogleAPIError):
        return str(exc)

    where = f" «{sheet_name}»" if sheet_name else ""
    status = _http_status(exc)
    sa_email = google_manager.get_service_account_email()
    share_hint = f" Расшарьте файл на `{sa_email}`." if sa_email else ""

    # gspread поднимает свои исключения до того, как HTTP-код успеет дойти
    if isinstance(exc, gspread_exceptions.WorksheetNotFound):
        return (f"❌ Лист{where} не найден в таблице. Проверьте название листа "
                f"в .env (переменные FT_SHEET_NAME / RW_SHEET_NAME).")
    if isinstance(exc, gspread_exceptions.SpreadsheetNotFound):
        return (f"❌ Таблица не найдена по ID{where}. Проверьте ID в .env и то, "
                f"что файл расшарен сервис-аккаунту.{share_hint}")
    if _GSPREAD_NO_CREDS is not None and isinstance(exc, _GSPREAD_NO_CREDS):
        return "❌ Не найдены учётные данные Google. Задайте GOOGLE_CREDS_JSON или GOOGLE_CREDS_PATH."

    if status == 403:
        return (f"❌ Нет доступа к таблице{where} (403 Permission denied). "
                f"Файл не расшарен сервис-аккаунту бота.{share_hint}")
    if status == 404:
        return (f"❌ Таблица{where} не найдена (404). Неверный ID файла, "
                f"либо он удалён, либо не расшарен сервис-аккаунту.{share_hint}")
    if status == 401:
        return "❌ Учётные данные Google отклонены (401). Проверьте GOOGLE_CREDS_JSON."
    if status == 429:
        return "❌ Квота Google API исчерпана (429). Повторите попытку через минуту."
    if status == 400:
        return f"❌ Google API отклонил запрос (400) при работе с{where}. Проверьте формат данных в таблице."
    if status == 500 or status == 503:
        return f"❌ Сервис Google временно недоступен ({status}). Повторите попытку позже."
    if status:
        return f"❌ Ошибка Google API при работе с{where} (HTTP {status}). Подробности в логе бота."
    return f"❌ Не удалось выполнить операцию с{where}. Подробности в логе бота."


class GoogleManager:
    def __init__(self):
        self.base_dir = os.path.dirname(os.path.abspath(__file__))
        
        creds_json = os.getenv('GOOGLE_CREDS_JSON')
        if creds_json and creds_json.strip():
            self.creds_path = os.path.join(self.base_dir, 'credentials_tmp.json')
            try:
                with open(self.creds_path, 'w', encoding='utf-8') as f:
                    f.write(creds_json)
                logger.info("Credentials created from environment variable.")
            except Exception as e:
                logger.error(f"Failed to create credentials file from env: {e}")
        else:
            env_path = os.getenv('GOOGLE_CREDS_PATH')
            if env_path and env_path.strip():
                self.creds_path = env_path
            else:
                self.creds_path = os.path.join(self.base_dir, 'credentials.json')

        self._drive_service = None
        self._gc = None

        # Кэш значений таблиц: {(sheet_id, sheet_name): (timestamp, values)}
        self.cache_ttl = float(os.getenv('SHEET_CACHE_TTL', 20))
        self._cache = {}
        # Кэш worksheet-объектов (open_by_key тоже ходит в API)
        self._worksheets = {}
        self._cache_lock = threading.Lock()

    def get_drive_service(self):
        if self._drive_service is None:
            try:
                creds = Credentials.from_service_account_file(self.creds_path)
                scoped_creds = creds.with_scopes(['https://www.googleapis.com/auth/drive'])
                self._drive_service = build('drive', 'v3', credentials=scoped_creds)
            except Exception as e:
                logger.error(f"Google Drive API Error: {e}")
        return self._drive_service

    def get_forms_service(self):
        """Google Forms API — недоступен для управления правами.

        В Forms API v1 НЕТ ресурса permissions (в discovery-документе только
        forms.create / forms.get / forms.batchUpdate / setPublishSettings),
        поэтому вызов forms().permissions() падает с AttributeError.
        Права на форму — как и на любой файл Drive — выдаются через Drive API.
        Метод оставлен только для совместимости и явно сообщает об этом.
        """
        raise NotImplementedError(
            'Forms API v1 не управляет правами доступа к форме; '
            'используйте Drive API (permissions) для файлов-форм.'
        )

    def add_form_permission(self, form_id, email, role='writer'):
        """Выдать доступ к Google Form через Drive API.

        Форма — обычный файл Google Drive, поэтому права на неё выдаются
        тем же permissions().create(), что и на таблицу. Возвращает True
        при успехе, иначе пробрасывает исключение наружу.
        """
        service = self.get_drive_service()
        if service is None:
            raise RuntimeError('Drive API service is unavailable (see log)')
        service.permissions().create(
            fileId=form_id,
            body={'type': 'user', 'role': role, 'emailAddress': email},
            sendNotificationEmail=False,
        ).execute()
        return True

    def revoke_permission(self, file_id, email):
        """Отозвать доступ к файлу Drive (таблица или форма) по email.

        permissions().list() отдаёт максимум 100 записей за страницу —
        без перебора nextPageToken на «толстом» файле нужный email может
        оказаться на второй странице и отзыв молча не сработает.
        Возвращает True, если право найдено и удалено, иначе False.
        Ошибки API пробрасываются наружу — вызывающий код сам решает,
        как их показать (больше не "глотаются" молча).
        """
        service = self.get_drive_service()
        if service is None:
            raise RuntimeError('Drive API service is unavailable (see log)')
        target = email.strip().lower()
        removed = False
        page_token = None
        while True:
            resp = service.permissions().list(
                fileId=file_id,
                fields='permissions(id, emailAddress), nextPageToken',
                pageSize=100,
                pageToken=page_token,
            ).execute()
            for p in resp.get('permissions', []):
                if (p.get('emailAddress') or '').strip().lower() == target:
                    service.permissions().delete(
                        fileId=file_id,
                        permissionId=p['id'],
                    ).execute()
                    removed = True
            page_token = resp.get('nextPageToken')
            if not page_token:
                break
        return removed

    def remove_form_permission(self, form_id, email):
        """Отозвать доступ к Google Form через Drive API."""
        return self.revoke_permission(form_id, email)

    def link_writer_access(self, file_id):
        """('anyone'|'domain', role), если файл открыт по ссылке на запись.

        Главная скрытая причина «доступ не отзывается вообще»: при
        ссылочном доступе на редактирование удаление права конкретного
        email ничего не меняет — доступ остаётся через ссылку. Возвращает
        None, если ссылочного доступа на запись нет или запрос не удался
        (проверка диагностическая и молча деградирует).
        """
        service = self.get_drive_service()
        if service is None:
            return None
        try:
            page_token = None
            while True:
                resp = service.permissions().list(
                    fileId=file_id,
                    fields='permissions(type, role), nextPageToken',
                    pageSize=100,
                    pageToken=page_token,
                ).execute()
                for p in resp.get('permissions', []):
                    if p.get('type') in ('anyone', 'domain') and p.get('role') == 'writer':
                        return p.get('type'), p.get('role')
                page_token = resp.get('nextPageToken')
                if not page_token:
                    break
        except Exception as e:
            logger.error(f"Link access check failed for {file_id}: {e}")
        return None

    def get_service_account_email(self):
        """Email сервис-аккаунта — его нужно добавить в файлы Google.

        Используется в подсказках при 403/404: менеджер сразу видит,
        кому расшарить таблицу. Пустая строка, если файл не читается.
        """
        try:
            with open(self.creds_path, encoding='utf-8') as f:
                return (json.load(f).get('client_email') or '').strip()
        except Exception:
            return ''

    def get_gc(self):
        if self._gc is None:
            try:
                self._gc = gspread.service_account(filename=self.creds_path)
            except Exception as e:
                logger.error(f"Gspread Error: {e}")
        return self._gc

    def get_sheet(self, sheet_id, sheet_name, reraise=False):
        """Открывает worksheet. reraise=True поднимает GoogleAPIError
        с человекочитаемым текстом вместо возврата None."""
        try:
            gc = self.get_gc()
            if gc is None:
                raise GoogleAPIError(
                    "❌ Не удалось инициализировать Google-подключение "
                    "(см. лог). Проверьте GOOGLE_CREDS_JSON / GOOGLE_CREDS_PATH.")
            return gc.open_by_key(sheet_id).worksheet(sheet_name)
        except GoogleAPIError:
            raise
        except Exception as e:
            logger.error(f"Error getting sheet {sheet_name}: {e}", exc_info=True)
            if reraise:
                raise GoogleAPIError(describe_google_error(e, sheet_name)) from e
            return None

    # ===== КЭШ ТАБЛИЦ =====
    # Одно нажатие кнопки раньше делало 2-4 запроса к Sheets API
    # (col_values + row_values, затем повторный reload для эмбеда).
    # Теперь лист читается одним get_all_values() и переиспользуется
    # в течение SHEET_CACHE_TTL секунд. Записи инвалидируют кэш.

    def get_table(self, sheet_id, sheet_name, fresh=False, reraise=False):
        """Все значения листа (list of lists) с кэшем по TTL.

        fresh=True обходит кэш и обновляет его — используется внутри
        критической секции записи, чтобы read-modify-write не опирался
        на устаревшие данные. При ошибке подключения возвращает None,
        а с reraise=True поднимает GoogleAPIError с понятным текстом.
        """
        key = (sheet_id, sheet_name)
        if not fresh:
            with self._cache_lock:
                entry = self._cache.get(key)
                if entry and (time.monotonic() - entry[0]) < self.cache_ttl:
                    return entry[1]

        sheet = self.get_sheet(sheet_id, sheet_name, reraise=reraise)
        if sheet is None:
            return None
        try:
            values = sheet.get_all_values()
        except Exception as e:
            logger.error(f"Error reading sheet {sheet_name}: {e}", exc_info=True)
            if reraise:
                raise GoogleAPIError(describe_google_error(e, sheet_name)) from e
            return None

        with self._cache_lock:
            self._cache[key] = (time.monotonic(), values)
        return values

    def invalidate_table(self, sheet_id, sheet_name):
        """Сбрасывает кэш листа (после записи)."""
        with self._cache_lock:
            self._cache.pop((sheet_id, sheet_name), None)

    def invalidate_all(self):
        with self._cache_lock:
            self._cache.clear()

    def _patch_cache(self, sheet_id, sheet_name, updates):
        """Обновляет закэшированную таблицу записанными значениями.

        Позволяет перечитать сотрудника из кэша сразу после записи,
        не делая лишнего запроса к API. Если кэша нет — не создаёт его.
        """
        key = (sheet_id, sheet_name)
        with self._cache_lock:
            entry = self._cache.get(key)
            if not entry:
                return
            values = [list(row) for row in entry[1]]
            for row, col, value in updates:
                while len(values) < row:
                    values.append([])
                target = values[row - 1]
                while len(target) < col:
                    target.append('')
                target[col - 1] = value
            self._cache[key] = (entry[0], values)

    def write_cells(self, sheet_id, sheet_name, updates):
        """Пишет несколько ячеек ОДНИМ запросом (batch_update).

        updates: список кортежей (row, col, value), нумерация с 1.
        Один вызов вместо N update_cell — меньше шансов, что параллельная
        запись встанет между нашими обновлениями, и меньше расход квоты.
        После записи кэш патчится записанными значениями.
        Исключения пробрасываются наружу — вызывающий код их показывает.
        """
        if not updates:
            return None
        sheet = self.get_sheet(sheet_id, sheet_name, reraise=True)

        data = [{'range': rowcol_to_a1(row, col), 'values': [[value]]}
                for row, col, value in updates]
        try:
            result = sheet.batch_update(data)
        except GoogleAPIError:
            raise
        except Exception as e:
            raise GoogleAPIError(describe_google_error(e, sheet_name)) from e
        self._patch_cache(sheet_id, sheet_name, updates)
        return result


def normalize_nick(nick: str) -> str:
    """Срезает клан-теги вида [TAG], (TAG), {TAG} и лишние пробелы."""
    if not nick:
        return ""
    return re.sub(r'\[[^\]]*\]|\([^)]*\)|\{[^}]*\}', '', nick).strip()


def parse_points(value) -> int:
    if not value:
        return 0
    s = str(value).strip().replace(' ', '').replace(',', '').replace('\xa0', '')
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return 0


def parse_punishment(value) -> int:
    """Достаёт число наказаний из строки вида '2/3'.

    Не падает на пустых/битых значениях ('', '—', 'нет') — возвращает 0.
    """
    if not value:
        return 0
    head = str(value).strip().split('/')[0].strip()
    try:
        return max(0, int(float(head)))
    except (TypeError, ValueError):
        return 0


# Singleton instance for the app
google_manager = GoogleManager()


# ===== БЛОКИРОВКИ ЗАПИСИ =====
# Снятие наказания — это read-modify-write: если два менеджера нажмут
# одновременно, второй перезапишет результат первого и одно наказание
# потеряется. Лок сериализует операции по конкретному сотруднику
# (дивизион + нормализованный ник), а внутри критической секции данные
# читаются заново (fresh=True), поэтому запись идёт от актуальных значений.
# asyncio.Lock в Python 3.10+ не привязан к loop'у в момент создания.

# Реестр локов с refcount: запись удаляется, когда последний пользователь
# освободил лок, — словарь больше не растёт без предела. Refcount
# инкрементируется до ожидания лока, поэтому «чужой» ожидающий не даст
# удалить запись и подменить лок параллельной операции.
_staff_locks = {}  # {(division, nick): (asyncio.Lock, refcount)}
_staff_locks_guard = threading.Lock()


@contextlib.asynccontextmanager
async def staff_lock(division: str, nick: str):
    """Персональный лок сотрудника. Использовать как 'async with'."""
    key = (division, normalize_nick(nick).lower())
    with _staff_locks_guard:
        lock, refs = _staff_locks.get(key) or (asyncio.Lock(), 0)
        _staff_locks[key] = (lock, refs + 1)
    try:
        async with lock:
            yield
    finally:
        with _staff_locks_guard:
            lock, refs = _staff_locks[key]
            refs -= 1
            if refs > 0:
                _staff_locks[key] = (lock, refs)
            else:
                del _staff_locks[key]


def division_ids(division: str):
    """(sheet_id, sheet_name, form_id) для дивизиона."""
    if division == 'FT':
        return FT_GOOGLE_SHEETS_ID, FT_SHEET_NAME, FT_GOOGLE_FORM_ID
    return RW_GOOGLE_SHEETS_ID, RW_SHEET_NAME, RW_GOOGLE_FORM_ID


def _row_to_user(values, row_num):
    """Собирает dict сотрудника из строки таблицы (нумерация с 1)."""
    row = values[row_num - 1] if row_num - 1 < len(values) else []
    return {
        'row': row_num,
        'nick': row[0] if len(row) > 0 else '',
        'position': row[1] if len(row) > 1 else '',
        'points': row[2] if len(row) > 2 else '0',
        'warns': row[3] if len(row) > 3 else '0/3',
        'warnings': row[4] if len(row) > 4 else '0/3',
        'email': row[5] if len(row) > 5 else '',
    }


def find_user_by_nick(nick, sheet_id, sheet_name, fresh=False, reraise=False):
    """Ищет сотрудника по нику в указанной таблице дивизиона.

    Один запрос к API на поиск (раньше было два: col_values + row_values),
    повторные вызовы в пределах TTL берутся из кэша. fresh=True обходит кэш.
    reraise=True поднимает GoogleAPIError с понятным текстом вместо возврата
    None при недоступной таблице (None остаётся для «сотрудник не найден»).
    Ищет только в строках состава (см. sheet_start_row) — служебные строки
    листа совпасть не могут.
    Синхронная — из асинхронного кода вызывать через asyncio.to_thread.
    """
    clean_nick = normalize_nick(nick)
    if not clean_nick:
        return None
    values = google_manager.get_table(sheet_id, sheet_name, fresh=fresh, reraise=reraise)
    if values is None:
        return None
    target = clean_nick.lower()
    start = sheet_start_row(sheet_id, sheet_name)
    for row_num, row in enumerate(values, start=1):
        if row_num < start or not row:
            continue
        if normalize_nick(row[0]).lower() != target:
            continue
        # Служебные строки (заголовки, «Вакантно», даты) совпасть не могут;
        # баллы не требуем — сотрудник с пустой колонкой баллов тоже ищется.
        if is_staff_row(row, require_points=False):
            return _row_to_user(values, row_num)
    return None


# Заголовки секций состава («Высшая Администрация и Высшая Модерация
# проекта», «Хелперы проекта»), «Вакантно» с опечатками, «Дата» и сами
# даты — всё это стоит в колонке ников, но сотрудниками не является.
# Список точных слов такой мусор не ловит (опечатки, новые формулировки),
# поэтому фильтр структурный: сотрудник — это строка, у которой заполнены
# и ник, и баллы.

# Строки-даты: «21.10.2025», «2025-10-21», «21/10», «10.2025». Проверяются
# ДО числового разбора, иначе «21.10.2025» распознался бы как число 21.
_DATE_RE = re.compile(r'^\d{1,4}([./-]\d{1,4}){1,3}$')

# Слова, которые в колонке ников сотрудниками не являются, даже если
# рядом проставлены баллы («Вакантно», «вакансия», «vacant», «пусто», «нет»).
_SKIP_WORDS = {'вакантно', 'вакансия', 'vacant', 'пусто', 'нет', 'none', '-'}


def _is_number(value) -> bool:
    """True, если значение колонки баллов — число («1 000», «1,5», «0»)."""
    try:
        int(float(str(value).strip().replace(' ', '').replace(',', '')
                  .replace('\xa0', '')))
        return True
    except (TypeError, ValueError):
        return False


def is_staff_row(row, require_points=True) -> bool:
    """True, если строка листа — реальный сотрудник, а не служебная.

    row — сырая строка таблицы (list). Сотрудник обязан иметь непустой ник
    (после срезания клан-тегов) и не быть словом-заглушкой или датой.
    Дальше два режима:
    - require_points=True (списки: /top, /stat, автокомплит): колонка
      баллов обязана быть числом. У заголовков секций, «Вакантно» и дат
      она пуста или нечисловая, а у сотрудников там всегда число (хотя бы 0).
    - require_points=False (поиск по нику для /staff): баллов может не
      быть, но тогда должна быть заполнена хотя бы одна другая колонка
      (должность, наказания, почта). Пустая строка-заглушка с опечаткой
      («Вакантто») так отсеивается, а сотрудник с незаполненными баллами
      не становится «невидимым» для карточки.
    """
    if not row:
        return False
    clean = normalize_nick(row[0]).strip()
    if not clean:
        return False
    if clean.lower() in _SKIP_WORDS:
        return False
    if _DATE_RE.match(clean):
        return False
    points_raw = row[2] if len(row) > 2 else ''
    if _is_number(points_raw):
        return True
    if require_points:
        return False
    return any(str(cell).strip() for cell in row[1:])


def sheet_start_row(sheet_id, sheet_name) -> int:
    """Номер строки (с 1), с которой в этом листе начинаются сотрудники.

    Всё выше — шапка и служебные строки — не парсится. Граница задаётся
    в config (FT_SHEET_START_ROW / RW_SHEET_START_ROW) и настраивается
    через .env, если листы перестроят.
    """
    if (sheet_id, sheet_name) == (FT_GOOGLE_SHEETS_ID, FT_SHEET_NAME):
        return FT_SHEET_START_ROW
    if (sheet_id, sheet_name) == (RW_GOOGLE_SHEETS_ID, RW_SHEET_NAME):
        return RW_SHEET_START_ROW
    return 1


def list_nicks(sheet_id, sheet_name):
    """Все ники сотрудников дивизиона (для автокомплита /staff).

    Служебные записи и строки выше границы состава исключаются
    (см. is_staff_row).
    """
    values = google_manager.get_table(sheet_id, sheet_name)
    if values is None:
        return []
    start = sheet_start_row(sheet_id, sheet_name)
    nicks = []
    for row_num, row in enumerate(values, start=1):
        if row_num < start or not row:
            continue
        if is_staff_row(row):
            nicks.append(normalize_nick(row[0]))
    return nicks


# ===== ПОТОЛКИ НАКАЗАНИЙ (Ф6) =====

def punishment_limits(division: str):
    """(warn_limit, strike_limit) для дивизиона — потолки из config."""
    if division == 'FT':
        return FT_WARN_LIMIT, FT_STRIKE_LIMIT
    return RW_WARN_LIMIT, RW_STRIKE_LIMIT


def collect_staff(sheet_id, sheet_name, reraise=False):
    """Все сотрудники дивизиона с разобранными числами.

    Один список строк вместо повторных обходов таблицы — из него строятся
    и /top (лидерборд по баллам), и /stat (карточка одного сотрудника).
    Служебные строки (заголовки секций, «Вакантно», даты) исключаются
    структурным фильтром is_staff_row.
    Каждый элемент: {nick, position, points, warns, strikes, warn_limit,
    strike_limit, email, row}.
    """
    values = google_manager.get_table(sheet_id, sheet_name, reraise=reraise)
    if values is None:
        return []
    start = sheet_start_row(sheet_id, sheet_name)
    staff = []
    for row_num, row in enumerate(values, start=1):
        if row_num < start or not row:
            continue
        if not is_staff_row(row):
            continue
        user = _row_to_user(values, row_num)
        staff.append({
            'row': row_num,
            'nick': normalize_nick(row[0]),
            'position': user.get('position', ''),
            'email': user.get('email', ''),
            'points': parse_points(user.get('points')),
            'warns': parse_punishment(user.get('warns')),
            'strikes': parse_punishment(user.get('warnings')),
        })
    return staff
