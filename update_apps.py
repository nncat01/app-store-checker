#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Реестр приложений App Store -> CSV (для автообновления в GitHub-репозитории)

Источник входных данных — links.txt. Поддерживает:

  1. Обычные ссылки на приложение (или голый App ID), по одной на строку.
  2. Ссылки на страницу разработчика — скрипт подтянет ВСЕ его приложения
     одним запросом к iTunes Lookup API (entity=software).
  3. Комментарии к приложениям — попадают в столбец "Комментарий":
       - для одного приложения:  <ссылка> commit: текст комментария
       - для группы (пока не встретится пустая строка):
             commit: текст для всей группы
             <ссылка 1>
             <ссылка 2>
     Если для приложения есть и групповой, и персональный комментарий —
     оба попадают в ячейку через пробел, сначала групповой, потом личный.
     То же самое работает и для ссылок на разработчика.
     Если одно и то же приложение явно указано напрямую И через ссылку на
     его разработчика — дублей в таблице не будет, и используется именно
     комментарий прямой ссылки (комментарий разработчика для этого
     конкретного приложения игнорируется).
  4. Свободные комментарии в links.txt через "#" в начале строки — просто
     игнорируются, ни на что не влияют (это для твоих собственных пометок).

Про даты пропажи из региона: если скрипт застал момент, когда приложение
было доступно, а потом стало недоступно — дата фиксируется точно. Если
скрипт с самого начала видит регион как недоступный (то есть исчезновение
произошло ДО того, как ссылка попала в links.txt) — дата не ставится
вообще, только "✗" без скобок, потому что реальная дата неизвестна.
Такую дату можно вписать вручную прямо в CSV в формате "✗ (ДД.ММ.ГГГГ)" —
скрипт её не тронет и не перезапишет, пока регион не станет снова доступен.

Таблицу можно редактировать руками: строки приложений, которых нет в
links.txt, скрипт не трогает вообще — так можно вручную завести запись
о приложении, которого больше нет ни в одном сторе.

Установка зависимостей:
    pip install requests

Использование:
    python update_apps.py                       # links.txt -> apps.csv
    python update_apps.py --links other.txt --file other.csv
    python update_apps.py --no-regions           # без проверки регионов (быстрее)
"""

import argparse
import csv
import re
import sys
import time
from datetime import date
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Не найден пакет 'requests'. Установи: pip install requests")


# ---------------------------------------------------------------------------
# Регионы: 1) приоритетные RU/TR/IN/US/KZ, 2) СНГ по алфавиту (+Украина),
# 3) ЕС по алфавиту, 4) Гонконг/Китай/Тайвань в этом порядке, 5) остальное
# по алфавиту. Меняешь список стран — правь эти пять групп.
# ---------------------------------------------------------------------------
_PRIORITY = [("ru", "Россия"), ("tr", "Турция"), ("in", "Индия"), ("us", "США"), ("kz", "Казахстан")]

_CIS_RU = {
    "am": "Армения", "az": "Азербайджан", "by": "Беларусь", "kg": "Киргизия",
    "tj": "Таджикистан", "tm": "Туркмения", "ua": "Украина", "uz": "Узбекистан",
}
_CIS = [(c, _CIS_RU[c]) for c in sorted(_CIS_RU)]

_EU_RU = {
    "de": "Германия", "ee": "Эстония", "fr": "Франция", "it": "Италия",
    "lt": "Литва", "lv": "Латвия", "pl": "Польша",
}
_EU = [(c, _EU_RU[c]) for c in sorted(_EU_RU)]

_GREATER_CHINA = [("hk", "Гонконг"), ("cn", "Китай"), ("tw", "Тайвань")]

_REST_RU = {
    "br": "Бразилия", "ca": "Канада", "ge": "Грузия",
    "il": "Израиль", "jp": "Япония", "za": "ЮАР",
}
_REST = [(c, _REST_RU[c]) for c in sorted(_REST_RU)]

REGION_COUNTRIES = _PRIORITY + _CIS + _EU + _GREATER_CHINA + _REST

# Пауза между запросами при проверке регионов. У публичного iTunes API нет
# официальной документации по лимитам, но неофициально он глушит примерно
# после 20 запросов в минуту с одного IP. 3.5 сек держит нас безопасно ниже
# границы даже с учётом сетевых задержек и большого числа регионов (29).
REGION_REQUEST_DELAY = 3.5

STATUS_AVAILABLE = "✓"
STATUS_UNAVAILABLE = "✗"
STATUS_UNKNOWN = "?"
OS_MARK = {True: "✓", False: "✗", None: "?"}

HEADERS = [
    "Название (App Store)",
    "Комментарий",
    "App ID",
    "Bundle ID",
    "Разработчик",
    "Версия",
    "Мин. iOS",
    "iOS",
    "iPadOS",
    "Размер (МБ)",
    "Добавлено",
] + [code.upper() for code, _ in REGION_COUNTRIES]

LOOKUP_URL = "https://itunes.apple.com/lookup"


# ---------------------------------------------------------------------------
# Разбор ссылок / определение платформ
# ---------------------------------------------------------------------------

def detect_os_support(supported_devices):
    """По полю supportedDevices ('iPhone16,2', 'iPadAir-iPadAir' и т.п.)
    определяет поддержку iPhone/iOS и iPad/iPadOS. Возвращает (ios, ipados);
    (None, None), если Apple вообще не отдала это поле — тогда честно '?'."""
    if not supported_devices:
        return None, None
    has_iphone = any(d.startswith(("iPhone", "iPod")) for d in supported_devices)
    has_ipad = any(d.startswith("iPad") for d in supported_devices)
    return has_iphone, has_ipad


def classify_link(raw: str):
    """Определяет тип ссылки. Возвращает:
      ("app", app_id, country) — обычное приложение (или голый App ID)
      ("developer", developer_id, country) — страница разработчика
      None — не смог разобрать
    Проверка на разработчика идёт ПЕРЕД обычным приложением, потому что
    ссылка на разработчика тоже содержит "id<цифры>" в конце."""
    text = raw.strip()
    if not text:
        return None
    if text.isdigit():
        return "app", text, None

    dev_match = re.search(r"apple\.com/([a-z]{2})/developer/[^/]+/id(\d+)", text, re.IGNORECASE)
    if dev_match:
        return "developer", dev_match.group(2), dev_match.group(1).lower()

    id_match = re.search(r"id(\d{5,})", text, re.IGNORECASE)
    if not id_match:
        return None
    country_match = re.search(r"apple\.com/([a-z]{2})/", text, re.IGNORECASE)
    country = country_match.group(1).lower() if country_match else None
    return "app", id_match.group(1), country


# ---------------------------------------------------------------------------
# Запросы к iTunes Lookup API
# ---------------------------------------------------------------------------

def parse_app_result(r: dict) -> dict:
    """Превращает один объект результата (wrapperType == 'software') в наш
    внутренний словарь с данными о приложении."""
    size_bytes = r.get("fileSizeBytes")
    size_mb = round(int(size_bytes) / (1024 * 1024), 1) if size_bytes else "—"
    ios_ok, ipados_ok = detect_os_support(r.get("supportedDevices"))
    return {
        "name": r.get("trackName", "Без названия"),
        "app_id": str(r.get("trackId")),
        "bundle_id": r.get("bundleId", "—"),
        "developer": r.get("artistName", "Неизвестно"),
        "version": r.get("version", "—"),
        "min_ios": r.get("minimumOsVersion", "—"),
        "size_mb": size_mb,
        "ios": ios_ok,
        "ipados": ipados_ok,
    }


def fetch_app_info(app_id: str, country: str | None):
    """Возвращает dict с данными о приложении или None, если оно нигде не
    нашлось (ни в указанной стране, ни в US-фолбэке). Поднимает
    requests.RequestException при проблемах с сетью."""
    for c in [country or "us"] + ([] if country in (None, "us") else ["us"]):
        resp = requests.get(LOOKUP_URL, params={"id": app_id, "country": c}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("resultCount"):
            info = parse_app_result(data["results"][0])
            info["_resolved_country"] = c
            return info
    return None


def fetch_developer_apps(developer_id: str, country: str | None):
    """Возвращает список info-словарей для всех приложений разработчика по
    ОДНОМУ запросу (entity=software), либо None, если разработчик нигде не
    нашёлся. Поднимает requests.RequestException при проблемах с сетью."""
    for c in [country or "us"] + ([] if country in (None, "us") else ["us"]):
        resp = requests.get(
            LOOKUP_URL,
            params={"id": developer_id, "country": c, "entity": "software"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        apps = [parse_app_result(r) for r in data.get("results", []) if r.get("wrapperType") == "software"]
        if apps:
            for a in apps:
                a["_resolved_country"] = c
            return apps
    return None


def check_region_availability(app_id: str, known: dict | None = None) -> dict:
    """Проверяет доступность в REGION_COUNTRIES. По одному запросу на регион
    (кроме уже известных из `known`), с паузой между запросами. Возвращает
    {код: True/False/None}, где None значит 'не удалось проверить сейчас'."""
    known = known or {}
    result = {}
    for code, name_ru in REGION_COUNTRIES:
        if code in known:
            result[code] = known[code]
            continue
        try:
            resp = requests.get(LOOKUP_URL, params={"id": app_id, "country": code}, timeout=10)
            result[code] = resp.ok and bool(resp.json().get("resultCount"))
        except (requests.RequestException, ValueError):
            result[code] = None
        print(f"      {code.upper():<3} {name_ru:<12} {OS_MARK[result[code]]}")
        time.sleep(REGION_REQUEST_DELAY)
    return result


def run_region_check(info: dict, check_regions: bool) -> dict:
    if not check_regions:
        return {}
    resolved = info.get("_resolved_country")
    known = {resolved: True} if resolved in dict(REGION_COUNTRIES) else {}
    eta_min = round(len(REGION_COUNTRIES) * REGION_REQUEST_DELAY / 60, 1)
    print(f"    Проверяю {len(REGION_COUNTRIES)} регионов (~{eta_min} мин)...")
    return check_region_availability(info["app_id"], known=known)


# ---------------------------------------------------------------------------
# links.txt: ссылки, группы, комментарии
# ---------------------------------------------------------------------------

_GROUP_RE = re.compile(r"^commit:\s*(.*)$", re.IGNORECASE)
_INLINE_RE = re.compile(r"\s+commit:\s*(.*)$", re.IGNORECASE)


def parse_links_file(path: Path) -> list:
    """Читает links.txt и возвращает список записей:
    {"kind": "app"|"developer", "id": str, "country": str|None,
     "comment": str|None, "raw": исходная ссылка без комментария}.

    Правила:
      - пустая строка сбрасывает текущий групповой комментарий
      - строка "#..." — свободный комментарий, полностью игнорируется,
        группу не сбрасывает
      - строка вида "commit: текст" (и больше ничего) — начинает групповой
        комментарий для всех ссылок ниже, до пустой строки
      - "<ссылка> commit: текст" — личный комментарий для этой строки
      - если групповой и личный комментарии присутствуют одновременно —
        итоговый комментарий: "<групповой> <личный>"
    """
    if not path.exists():
        sys.exit(f"Файл со ссылками не найден: {path}")

    entries = []
    group_comment = None

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()

        if not line:
            group_comment = None
            continue
        if line.startswith("#"):
            continue

        m_group = _GROUP_RE.match(line)
        if m_group:
            group_comment = m_group.group(1).strip() or None
            continue

        m_inline = _INLINE_RE.search(line)
        if m_inline:
            link_part = line[:m_inline.start()].strip()
            inline_comment = m_inline.group(1).strip() or None
        else:
            link_part = line
            inline_comment = None

        parsed = classify_link(link_part)
        if parsed is None:
            print(f"  ⚠ Не понял строку в {path.name}: «{line}» — пропускаю.")
            continue

        kind, ident, country = parsed
        comment = " ".join(c for c in (group_comment, inline_comment) if c) or None
        entries.append({"kind": kind, "id": ident, "country": country, "comment": comment, "raw": link_part})

    return entries


# ---------------------------------------------------------------------------
# CSV: чтение, слияние с историей, запись
# ---------------------------------------------------------------------------

_CELL_RE = re.compile(r"^(✓|✗|\?)(?:\s*\((\d{2}\.\d{2}\.\d{4})\))?$")


def parse_region_cell(text: str):
    """Разбирает содержимое ячейки региона обратно на (доступно?, дата).
    '✓' -> (True, None); '✗' -> (False, None); '✗ (дата)' -> (False, 'дата');
    пустое/непонятное значение -> (None, None)."""
    text = (text or "").strip()
    m = _CELL_RE.match(text)
    if not m:
        return None, None
    mark, date_str = m.groups()
    if mark == STATUS_AVAILABLE:
        return True, None
    if mark == STATUS_UNAVAILABLE:
        return False, date_str
    return None, None


def build_region_cells(existing_row: dict, region_result: dict, today_text: str) -> dict:
    """Строит новые значения региональных колонок с учётом истории:
      - True  -> '✓' (дата снимается — приложение снова доступно)
      - False, а до этого было ИЗВЕСТНО, что доступно (True) ->
            '✗ (сегодня)' — застали момент пропажи, дата достоверна
      - False, а до этого уже было '✗ (дата)' -> дата не меняется
      - False, а до этого было пусто ИЛИ было '✗' без даты -> просто '✗',
            без даты — мы не знаем, когда реально пропало (можно вписать
            дату вручную в CSV в формате '✗ (ДД.ММ.ГГГГ)', тогда она уже
            не потеряется)
      - None (не проверяли сейчас/ошибка сети) -> оставляем как было
    """
    out = {}
    for code, _ in REGION_COUNTRIES:
        header = code.upper()
        new_result = region_result.get(code)
        prev_text = (existing_row or {}).get(header, "")

        if new_result is None:
            out[header] = prev_text
            continue
        if new_result is True:
            out[header] = STATUS_AVAILABLE
            continue

        prev_available, prev_date = parse_region_cell(prev_text)
        if prev_available is True:
            out[header] = f"{STATUS_UNAVAILABLE} ({today_text})"
        elif prev_available is False and prev_date:
            out[header] = f"{STATUS_UNAVAILABLE} ({prev_date})"
        else:
            out[header] = STATUS_UNAVAILABLE
    return out


def read_existing_csv(path: Path):
    """Возвращает (order: [app_id, ...], rows: {app_id: {заголовок: значение}}).
    restval="" защищает от ручных правок с неполным числом колонок."""
    if not path.exists():
        return [], {}
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, restval="")
        order, rows = [], {}
        for row in reader:
            app_id = (row.get("App ID") or "").strip()
            if not app_id:
                continue
            rows[app_id] = row
            order.append(app_id)
    return order, rows


def write_csv(path: Path, order: list, rows: dict):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=HEADERS, extrasaction="ignore")
        writer.writeheader()
        for app_id in order:
            writer.writerow(rows[app_id])


def build_row(info: dict, existing_row: dict, region_result: dict, comment: str | None) -> dict:
    today_text = date.today().strftime("%d.%m.%Y")
    row = {
        "Название (App Store)": info["name"],
        "Комментарий": comment or "",
        "App ID": info["app_id"],
        "Bundle ID": info["bundle_id"],
        "Разработчик": info["developer"],
        "Версия": info["version"],
        "Мин. iOS": info["min_ios"],
        "iOS": OS_MARK[info["ios"]],
        "iPadOS": OS_MARK[info["ipados"]],
        "Размер (МБ)": info["size_mb"],
        "Добавлено": (existing_row or {}).get("Добавлено") or today_text,
    }
    row.update(build_region_cells(existing_row, region_result, today_text))
    return row


# ---------------------------------------------------------------------------
# Обработка записей
# ---------------------------------------------------------------------------

def process_app_entry(entry: dict, order: list, rows: dict, check_regions: bool, claimed: set) -> bool:
    app_id_hint, country, comment = entry["id"], entry["country"], entry["comment"]

    try:
        info = fetch_app_info(app_id_hint, country)
    except requests.RequestException as exc:
        print(f"  ✗ Ошибка сети для {entry['raw']}: {exc}")
        return False

    if info is None:
        existing_row = rows.get(app_id_hint)
        if existing_row is None:
            print(f"  ✗ Приложение {app_id_hint} не найдено, и раньше в реестре его не было — пропускаю.")
            return False
        print(f"  ⚠ {app_id_hint} не отвечает ни в одном сторе — похоже, снято отовсюду.")
        print("    Обновляю отметки регионов и комментарий; остальные поля — как в последнем успешном запросе.")
        today_text = date.today().strftime("%d.%m.%Y")
        region_result = {code: False for code, _ in REGION_COUNTRIES}
        row = dict(existing_row)
        row["Комментарий"] = comment or ""
        row.update(build_region_cells(existing_row, region_result, today_text))
        rows[app_id_hint] = row
        claimed.add(app_id_hint)
        return True

    print(
        f"  Нашёл: {info['name']} v{info['version']} — {info['developer']} "
        f"({info['app_id']}, мин. iOS {info['min_ios']}, {info['size_mb']} МБ, "
        f"iOS {OS_MARK[info['ios']]} / iPadOS {OS_MARK[info['ipados']]})"
    )
    region_result = run_region_check(info, check_regions)

    if info["app_id"] not in rows:
        order.append(info["app_id"])
    rows[info["app_id"]] = build_row(info, rows.get(info["app_id"]), region_result, comment)
    claimed.add(info["app_id"])
    return True


def process_developer_entry(entry: dict, order: list, rows: dict, check_regions: bool, claimed: set) -> int:
    dev_id, country, comment = entry["id"], entry["country"], entry["comment"]

    try:
        apps = fetch_developer_apps(dev_id, country)
    except requests.RequestException as exc:
        print(f"  ✗ Ошибка сети для разработчика {entry['raw']}: {exc}")
        return 0

    if not apps:
        print(f"  ✗ Не нашёл приложений разработчика {dev_id} ({entry['raw']}).")
        return 0

    print(f"  Разработчик {dev_id}: найдено {len(apps)} приложени(й).")
    added = 0
    for info in apps:
        if info["app_id"] in claimed:
            print(f"    — {info['name']} уже учтено напрямую — пропускаю, комментарий аккаунта не применяется.")
            continue
        print(
            f"    {info['name']} v{info['version']} ({info['app_id']}, "
            f"iOS {OS_MARK[info['ios']]} / iPadOS {OS_MARK[info['ipados']]})"
        )
        region_result = run_region_check(info, check_regions)
        if info["app_id"] not in rows:
            order.append(info["app_id"])
        rows[info["app_id"]] = build_row(info, rows.get(info["app_id"]), region_result, comment)
        claimed.add(info["app_id"])
        added += 1
    return added


# ---------------------------------------------------------------------------
# Сортировка
# ---------------------------------------------------------------------------

def sort_order_alphabetically(order: list, rows: dict) -> list:
    """Сортирует список app_id по названию приложения (регистронезависимо).
    Затрагивает вообще все строки в rows — включая добавленные вручную,
    т.к. они тоже часть order/rows после read_existing_csv."""
    return sorted(order, key=lambda app_id: (rows[app_id].get("Название (App Store)") or "").casefold())


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Реестр приложений App Store -> CSV")
    parser.add_argument("--links", default="links.txt", help="Файл со ссылками (по умолчанию links.txt)")
    parser.add_argument("--file", default="apps.csv", help="Итоговый CSV-файл (по умолчанию apps.csv)")
    parser.add_argument("--no-regions", action="store_true", help="Не проверять регионы (быстрее)")
    args = parser.parse_args()

    links_path = Path(args.links)
    csv_path = Path(args.file)
    check_regions = not args.no_regions

    entries = parse_links_file(links_path)
    if not entries:
        sys.exit(f"В {links_path} нет ни одной ссылки.")

    order, rows = read_existing_csv(csv_path)
    claimed = set()

    app_entries = [e for e in entries if e["kind"] == "app"]
    dev_entries = [e for e in entries if e["kind"] == "developer"]
    print(f"Прямых ссылок: {len(app_entries)}. Ссылок на разработчиков: {len(dev_entries)}.")

    ok = failed = 0

    if app_entries:
        print("\n--- Приложения по прямым ссылкам ---")
    for entry in app_entries:
        tag = f"  [{entry['comment']}]" if entry["comment"] else ""
        print(f"\n{entry['raw']}{tag}")
        if process_app_entry(entry, order, rows, check_regions, claimed):
            ok += 1
            write_csv(csv_path, order, rows)  # сохраняем после каждого — не теряем прогресс при сбое
        else:
            failed += 1

    if dev_entries:
        print("\n--- Приложения по ссылкам на разработчиков ---")
    for entry in dev_entries:
        tag = f"  [{entry['comment']}]" if entry["comment"] else ""
        print(f"\n{entry['raw']}{tag}")
        added = process_developer_entry(entry, order, rows, check_regions, claimed)
        if added:
            ok += added
            write_csv(csv_path, order, rows)
        else:
            failed += 1

    print(f"\nГотово: {ok} ок, {failed} с ошибкой/пропуском.")

    order = sort_order_alphabetically(order, rows)
    write_csv(csv_path, order, rows)
    print(f"Таблица отсортирована по алфавиту. Файл: {csv_path.resolve()}")


if __name__ == "__main__":
    main()
