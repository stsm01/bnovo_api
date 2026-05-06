#!/usr/bin/env python3
"""
Bnovo PMS API — получение бронирований.

Использование:
    python3 bnovo.py                          # за последние 14 дней
    python3 bnovo.py --from 2026-03-01        # с даты по сегодня
    python3 bnovo.py --from 2026-03-01 --to 2026-03-31
    python3 bnovo.py --type checkmate         # по шахматке
    python3 bnovo.py --json                   # вывод сырого JSON
"""

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

# ─── Конфиг ──────────────────────────────────────────────────────────────────

BASE_URL = "https://api.pms.bnovo.ru"
CONFIG_FILE = Path(__file__).parent / ".bnovo_token.json"

# ─── HTTP ─────────────────────────────────────────────────────────────────────

def _request(method: str, path: str, *, body=None, token=None) -> dict:
    url = BASE_URL + path
    data = json.dumps(body).encode() if body else None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        try:
            err = json.loads(body_text)
        except Exception:
            err = {"raw": body_text}
        raise RuntimeError(f"HTTP {e.code}: {json.dumps(err, ensure_ascii=False)}")


def _get(path: str, params: dict, token: str) -> dict:
    qs = urllib.parse.urlencode(params)
    return _request("GET", f"{path}?{qs}", token=token)


def _post(path: str, body: dict) -> dict:
    return _request("POST", path, body=body)

# ─── Токен ────────────────────────────────────────────────────────────────────

def _jwt_exp(token: str) -> int:
    """Извлекает поле exp из JWT без верификации подписи."""
    try:
        payload_b64 = token.split(".")[1]
        # Дополняем padding
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return int(payload["exp"])
    except Exception:
        return 0


def _token_is_valid(token: str, margin_sec: int = 300) -> bool:
    """Токен считается валидным, если до истечения > margin_sec (5 минут)."""
    return _jwt_exp(token) - time.time() > margin_sec


def _load_token() -> str | None:
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text())
            token = data.get("access_token", "")
            if token and _token_is_valid(token):
                return token
        except Exception:
            pass
    return None


def _load_credentials() -> tuple[int, str]:
    """Загружает user_id и password из конфига."""
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text())
            uid = data.get("user_id", 0)
            pwd = data.get("password", "")
            if uid and pwd:
                return int(uid), pwd
        except Exception:
            pass
    return 0, ""


def _save_token(token: str, user_id: int = 0, password: str = ""):
    """Сохраняет токен и учётные данные в конфиг."""
    data = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text())
        except Exception:
            pass
    data["access_token"] = token
    if user_id:
        data["user_id"] = user_id
    if password:
        data["password"] = password
    CONFIG_FILE.write_text(json.dumps(data, indent=2))
    CONFIG_FILE.chmod(0o600)  # только владелец


def _authenticate(user_id: int, password: str) -> str:
    print("Получаем новый токен...", file=sys.stderr)
    resp = _post("/api/v1/auth", {"id": user_id, "password": password})
    token = resp["data"]["access_token"]
    _save_token(token, user_id, password)
    exp = _jwt_exp(token)
    print(
        f"Токен получен, действует до {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(exp))}",
        file=sys.stderr,
    )
    return token


def get_token(user_id: int, password: str) -> str:
    """Возвращает валидный токен: из кэша или свежий."""
    token = _load_token()
    if token:
        exp = _jwt_exp(token)
        print(
            f"Используем кэшированный токен (до {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(exp))})",
            file=sys.stderr,
        )
        return token
    return _authenticate(user_id, password)

# ─── Бронирования ─────────────────────────────────────────────────────────────

DATA_TYPES = {
    None: "все (по дате создания)",
    "checkmate": "по шахматке",
    "new": "новые",
    "living": "проживающие",
    "checkedIn": "заезд",
    "checkedOut": "выезд",
    "cancelled": "отменённые",
    "exited": "выехали",
    "changed": "изменённые",
}


def get_bookings(token: str, date_from: str, date_to: str, data_type: str | None = None) -> list[dict]:
    """Получает все бронирования за период (с пагинацией)."""
    all_bookings = []
    offset = 0
    limit = 50

    while True:
        params = {
            "date_from": date_from,
            "date_to": date_to,
            "limit": limit,
            "offset": offset,
        }
        if data_type:
            params["data_type"] = data_type

        resp = _get("/api/v1/bookings", params, token)
        chunk = resp["data"]["bookings"]
        total = resp["data"]["meta"]["total"]

        all_bookings.extend(chunk)
        offset += len(chunk)

        if offset >= total or not chunk:
            break

    return all_bookings

# ─── Вывод ────────────────────────────────────────────────────────────────────

def print_report(bookings: list[dict], date_from: str, date_to: str, data_type: str | None):
    type_label = DATA_TYPES.get(data_type, data_type)
    print(f"\n{'═'*70}")
    print(f"  Бронирования {date_from} — {date_to}  |  тип: {type_label}")
    print(f"  Всего: {len(bookings)}")
    print(f"{'═'*70}\n")

    if not bookings:
        print("Нет бронирований за указанный период.")
        return

    # Статистика
    statuses: dict[str, int] = {}
    sources: dict[str, int] = {}
    room_types: dict[str, dict] = {}
    total_amount = 0

    for b in bookings:
        statuses[b["status"]["name"]] = statuses.get(b["status"]["name"], 0) + 1
        sources[b["source"]["name"]] = sources.get(b["source"]["name"], 0) + 1
        total_amount += b["amount"]

        rt = b["prices"][0]["room_type_name"] if b.get("prices") else "—"
        if rt not in room_types:
            room_types[rt] = {"count": 0, "amount": 0}
        room_types[rt]["count"] += 1
        room_types[rt]["amount"] += b["amount"]

    def table(title, data: dict, value_label="Кол-во"):
        print(f"  {title}")
        for k, v in sorted(data.items(), key=lambda x: -x[1]):
            print(f"    {k:<40} {v}")
        print()

    table("По статусам:", statuses)
    table("По источникам:", sources)

    print("  По типам домов:")
    for rt, info in sorted(room_types.items(), key=lambda x: -x[1]["count"]):
        avg = info["amount"] // info["count"]
        print(f"    {rt:<40} {info['count']:>3} броней   {info['amount']:>12,} ₽  (ср. {avg:,} ₽)")
    print(f"\n  {'Итого:':<44} {len(bookings):>3} броней   {total_amount:>12,} ₽")
    print()

    # Список
    print(f"  {'Номер брони':<20} {'Гость':<28} {'Заезд':10} {'Выезд':10} {'Сумма':>10}  {'Статус':<12} Источник")
    print(f"  {'─'*110}")
    for b in bookings:
        c = b["customer"]
        name = f"{c.get('name', '')} {c.get('surname', '')}".strip()[:27]
        arr = b["dates"]["arrival"][:10]
        dep = b["dates"]["departure"][:10]
        print(
            f"  {b['number']:<20} {name:<28} {arr}  {dep}  {b['amount']:>10,}  "
            f"{b['status']['name']:<12} {b['source']['name']}"
        )
    print()

# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Bnovo API — получение бронирований")
    parser.add_argument("--from", dest="date_from", help="Дата начала (YYYY-MM-DD), по умолчанию -14 дней")
    parser.add_argument("--to", dest="date_to", help="Дата конца (YYYY-MM-DD), по умолчанию сегодня")
    parser.add_argument("--type", dest="data_type", choices=list(filter(None, DATA_TYPES)), help="Тип выборки")
    parser.add_argument("--json", dest="raw_json", action="store_true", help="Вывести сырой JSON")
    parser.add_argument("--id", dest="user_id", type=int, help="ID пользователя API")
    parser.add_argument("--password", dest="password", help="API ключ")
    args = parser.parse_args()

    # Даты
    today = date.today()
    date_to = args.date_to or today.isoformat()
    date_from = args.date_from or (today - timedelta(days=14)).isoformat()

    # Учётные данные: приоритет — аргументы → переменные окружения → конфиг
    user_id = args.user_id or int(os.environ.get("BNOVO_ID", 0))
    password = args.password or os.environ.get("BNOVO_PASSWORD", "")

    if not user_id or not password:
        cfg_uid, cfg_pwd = _load_credentials()
        user_id = user_id or cfg_uid
        password = password or cfg_pwd

    if not user_id or not password:
        # Попробуем получить токен из кэша без учётных данных
        token = _load_token()
        if not token:
            print(
                "Ошибка: укажите --id и --password, либо задайте переменные окружения "
                "BNOVO_ID и BNOVO_PASSWORD.",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        token = get_token(user_id, password)

    # Запрос с автоматическим обновлением токена при 401
    try:
        bookings = get_bookings(token, date_from, date_to, args.data_type)
    except RuntimeError as e:
        if "401" in str(e) and user_id and password:
            print("Токен отклонён (401), обновляем...", file=sys.stderr)
            token = _authenticate(user_id, password)
            bookings = get_bookings(token, date_from, date_to, args.data_type)
        else:
            raise

    if args.raw_json:
        print(json.dumps(bookings, ensure_ascii=False, indent=2))
    else:
        print_report(bookings, date_from, date_to, args.data_type)


if __name__ == "__main__":
    main()
