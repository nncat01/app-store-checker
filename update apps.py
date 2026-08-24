#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Реестр приложений App Store -> CSV (для автообновления в GitHub-репозитории)

Читает ссылки/App ID построчно из links.txt, для каждого приложения
запрашивает через открытый iTunes Lookup API: название, версию, размер,
поддержку iOS/iPadOS и доступность по списку регионов — и пишет/обновляет
строки в apps.csv.

Если приложение пропадает из региона, рядом с "✗" фиксируется дата первого
обнаружения пропажи в скобках — "✗ (24.08.2026)" — и дальше не
перезаписывается, пока приложение там снова не появится. Если приложение
вообще перестало отвечать (снято отовсюду), уже известная строка не
стирается — просто все регионы, где раньше было "✓", помечаются как "✗"
с сегодняшней датой, а остальные поля (название, версия и т.д.) остаются
как в последнем успешном запросе, потому что обновить их больше неоткуда.

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


def extract_id_and_country(raw: str):
    """Достаёт числовой App ID и (если есть) код страны из строки.
    Понимает голый ID ("284882215") и полную ссылку
    ("https://apps.apple.com/ru/app/name/id284882215?mt=8")."""
    text = raw.strip()
    if not text:
        return None
    if text.isdigit():
        return text, None
    id_match = re.search(r"id(\d{5,})", text, re.IGNORECASE)
    if not id_match:
        return None
    country_match = re.search(r"apple\.com/([a-z]{2})/", text, re.IGNORECASE)
    country = country_match.group(1).lower() if country_match else None
    return id_match.group(1), country


# ---------------------------------------------------------------------------
# Запросы к iTunes Lookup API
# ---------------------------------------------------------------------------

def fetch_app_info(app_id: str, country: str | None):
    """Возвращает dict с данными о приложении или None, если оно нигде не
    нашлось (ни в указанной стране, ни в US-фолбэке). Поднимает
    requests.RequestException при проблемах с сетью."""
    for c in [country or "us"] + ([] if country in (None, "us") else ["us"]):
        resp = requests.get(LOOKUP_URL, params={"id": app_id, "country": c}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("resultCount"):
            r = data["results"][0]
            size_bytes = r.get("fileSizeBytes")
            size_mb = round(int(size_bytes) / (1024 * 1024), 1) if size_bytes else "—"
            ios_ok, ipados_ok = detect_os_support(r.get("supportedDevices"))
            return {
                "name": r.get("trackName", "Без названия"),
                "app_id": str(r.get("trackId", app_id)),
                "bundle_id": r.get("bundleId", "—"),
                "developer": r.get("artistName", "Неизвестно"),
                "version": r.get("version", "—"),
                "min_ios": r.get("minimumOsVersion", "—"),
                "size_mb": size_mb,
                "ios": ios_ok,
                "ipados": ipados_ok,
                "_resolved_country": c,
            }
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
        print(f"    {code.upper():<3} {name_ru:<12} {OS_MARK[result[code]]}")
        time.sleep(REGION_REQUEST_DELAY)
    return result


# ---------------------------------------------------------------------------
# CSV: чтение, слияние с историей, запись
# ---------------------------------------------------------------------------

_CELL_RE = re.compile(r"^(✓|✗|\?)(?:\s*\((\d{2}\.\d{2}\.\d{4})\))?$")


def parse_region_cell(text: str):
    """Разбирает содержимое ячейки региона обратно на (доступно?, дата).
    Для '✓' -> (True, None); для '✗ (дата)' -> (False, 'дата');
    для пустого/непонятного значения -> (None, None)."""
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
    - True  -> '✓' (снимаем дату, если была — приложение снова доступно)
    - False -> '✗ (дата первого обнаружения)', дата не переписывается,
               если уже была зафиксирована раньше
    - None (не проверяли/ошибка сети) -> оставляем как было, ничего не теряем
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
        if prev_available is False and prev_date:
            out[header] = f"{STATUS_UNAVAILABLE} ({prev_date})"
        else:
            out[header] = f"{STATUS_UNAVAILABLE} ({today_text})"
    return out


def read_links(path: Path) -> list:
    if not path.exists():
        sys.exit(f"Файл со ссылками не найден: {path}")
    links = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        links.append(line)
    return links


def read_existing_csv(path: Path):
    """Возвращает (order: [app_id, ...], rows: {app_id: {заголовок: значение}})."""
    if not path.exists():
        return [], {}
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        order, rows = [], {}
        for row in reader:
            app_id = row.get("App ID", "").strip()
            if not app_id:
                continue
            rows[app_id] = row
            order.append(app_id)
    return order, rows


def write_csv(path: Path, order: list, rows: dict):
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=HEADERS)
        writer.writeheader()
        for app_id in order:
            writer.writerow(rows[app_id])


def build_row(info: dict, existing_row: dict, region_result: dict) -> dict:
    today_text = date.today().strftime("%d.%m.%Y")
    row = {
        "Название (App Store)": info["name"],
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
# Обработка одной ссылки
# ---------------------------------------------------------------------------

def process_link(raw_link: str, order: list, rows: dict, check_regions: bool = True) -> bool:
    parsed = extract_id_and_country(raw_link)
    if not parsed:
        print(f"  ✗ Не нашёл App ID в «{raw_link}». Пропускаю.")
        return False
    app_id_hint, country = parsed

    try:
        info = fetch_app_info(app_id_hint, country)
    except requests.RequestException as exc:
        print(f"  ✗ Ошибка сети: {exc}")
        return False

    existing_row = rows.get(app_id_hint)

    if info is None:
        if existing_row is None:
            print(f"  ✗ Приложение {app_id_hint} не найдено, и раньше в реестре его не было — пропускаю.")
            return False
        print(f"  ⚠ {app_id_hint} не отвечает ни в одном сторе — похоже, снято отовсюду.")
        print("    Обновляю только отметки регионов, остальные поля оставляю как в последнем успешном запросе.")
        today_text = date.today().strftime("%d.%m.%Y")
        region_result = {code: False for code, _ in REGION_COUNTRIES}
        row = dict(existing_row)
        row.update(build_region_cells(existing_row, region_result, today_text))
        rows[app_id_hint] = row
        return True

    print(
        f"  Нашёл: {info['name']} v{info['version']} — {info['developer']} "
        f"({info['app_id']}, мин. iOS {info['min_ios']}, {info['size_mb']} МБ, "
        f"iOS {OS_MARK[info['ios']]} / iPadOS {OS_MARK[info['ipados']]})"
    )

    region_result = {}
    if check_regions:
        resolved = info.get("_resolved_country")
        known = {resolved: True} if resolved in dict(REGION_COUNTRIES) else {}
        eta_min = round(len(REGION_COUNTRIES) * REGION_REQUEST_DELAY / 60, 1)
        print(f"  Проверяю {len(REGION_COUNTRIES)} регионов (~{eta_min} мин)...")
        region_result = check_region_availability(info["app_id"], known=known)

    if info["app_id"] not in rows:
        order.append(info["app_id"])
    rows[info["app_id"]] = build_row(info, existing_row, region_result)
    return True


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

    links = read_links(links_path)
    if not links:
        sys.exit(f"В {links_path} нет ни одной ссылки.")

    order, rows = read_existing_csv(csv_path)
    print(f"Обрабатываю {len(links)} ссылок(и) из {links_path}...")

    ok = failed = 0
    for link in links:
        print(f"\n{link}")
        if process_link(link, order, rows, check_regions=not args.no_regions):
            ok += 1
            write_csv(csv_path, order, rows)  # сохраняем после каждого — не теряем прогресс при сбое
        else:
            failed += 1

    print(f"\nГотово: {ok} ок, {failed} с ошибкой. Файл: {csv_path.resolve()}")


if __name__ == "__main__":
    main()
