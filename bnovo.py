#!/usr/bin/env python3
"""
Bnovo PMS API — бронирования, доп. услуги, счета.

Использование:
    python3 bnovo.py                                # бронирования за последние 14 дней
    python3 bnovo.py --from 2026-03-01              # с даты по сегодня
    python3 bnovo.py --from 2026-03-01 --to 2026-03-31
    python3 bnovo.py --type checkmate               # по шахматке
    python3 bnovo.py --json                         # вывод сырого JSON

    python3 bnovo.py --services                     # каталог доп. услуг отеля
    python3 bnovo.py --booking-services 397         # доп. услуги конкретного бронирования
    python3 bnovo.py --booking-invoices 397         # счета конкретного бронирования
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


def _load_cached_user_id() -> int:
    """Загружает user_id из кэша токена.

    API-ключ/пароль намеренно не читаем и не храним в кэше: это долгоживущий
    секрет. Для обновления токена ключ должен прийти из аргумента --password
    или переменной окружения BNOVO_PASSWORD.
    """
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text())
            uid = data.get("user_id", 0)
            if uid:
                return int(uid)
        except Exception:
            pass
    return 0


def _save_token(token: str, user_id: int = 0):
    """Сохраняет JWT-токен в кэш.

    Важно: API-ключ/пароль не сохраняется в файл. В кэше допускаются только
    краткоживущий access_token и несекретный user_id.
    """
    data = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text())
        except Exception:
            pass
    # Миграция старого формата: если раньше password был сохранён, удаляем.
    data.pop("password", None)
    data["access_token"] = token
    if user_id:
        data["user_id"] = user_id
    CONFIG_FILE.write_text(json.dumps(data, indent=2))
    CONFIG_FILE.chmod(0o600)  # только владелец


def _authenticate(user_id: int, password: str) -> str:
    print("Получаем новый токен...", file=sys.stderr)
    resp = _post("/api/v1/auth", {"id": user_id, "password": password})
    token = resp["data"]["access_token"]
    _save_token(token, user_id)
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

# ─── Доп. услуги ──────────────────────────────────────────────────────────────

def get_services(token: str) -> list[dict]:
    """Каталог доп. услуг отеля."""
    resp = _get("/api/v1/services", {}, token)
    return resp["data"]["services"]


def get_booking_services(token: str, booking_id: int) -> list[dict]:
    """Доп. услуги конкретного бронирования."""
    resp = _get(f"/api/v1/bookings/{booking_id}/services", {}, token)
    return resp["data"]["services"]


def get_booking_invoices(token: str, booking_id: int) -> list[dict]:
    """Счета конкретного бронирования."""
    resp = _get(f"/api/v1/bookings/{booking_id}/invoices", {}, token)
    return resp["data"]["invoices"]

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

def print_services(services: list[dict]):
    print(f"\n{'═'*70}")
    print(f"  Каталог доп. услуг  |  всего: {len(services)}")
    print(f"{'═'*70}\n")
    if not services:
        print("Нет доп. услуг.")
        return
    print(f"  {'ID':<6} {'Название':<35} {'Цена':>12}  {'Тип цены':<12} {'Категория'}")
    print(f"  {'─'*80}")
    for s in services:
        price = float(s.get("price", 0))
        price_type = s.get("price_type", "")
        stype = s.get("type", "")
        pkg = " [пакет]" if s.get("is_package") else ""
        print(f"  {s['id']:<6} {s['name']:<35} {price:>12,.2f}  {price_type:<12} {stype}{pkg}")
    print()


def print_booking_services(services: list[dict], booking_id: int):
    print(f"\n{'═'*70}")
    print(f"  Доп. услуги брони #{booking_id}  |  позиций: {len(services)}")
    print(f"{'═'*70}\n")
    if not services:
        print("Нет доп. услуг в этой брони.")
        return
    for s in services:
        included = "включена" if s.get("included") else "отдельно"
        qty_by_date = s.get("quantity", [])
        price_by_date = s.get("price", [])
        total = sum(
            list(d.values())[0]
            for d in (price_by_date if isinstance(price_by_date, list) else [])
            if d
        )
        print(f"  service_id={s['service_id']}  ({included})  итого: {total:,.0f} ₽")
        for entry in (qty_by_date if isinstance(qty_by_date, list) else []):
            for dt, qty in entry.items():
                price_entry = next(
                    (list(p.values())[0] for p in price_by_date if isinstance(p, dict) and dt in p),
                    "—",
                )
                print(f"    {dt}  qty={qty}  price={price_entry}")
    print()


def print_booking_invoices(invoices: list[dict], booking_id: int):
    print(f"\n{'═'*70}")
    print(f"  Счета брони #{booking_id}  |  всего: {len(invoices)}")
    print(f"{'═'*70}\n")
    if not invoices:
        print("Нет счетов для этой брони.")
        return
    for inv in invoices:
        paid = inv.get("payed_amount", 0)
        total = inv.get("amount", 0)
        debt = total - paid
        status = "оплачен" if debt <= 0 else f"долг {debt:,.0f} ₽"
        print(
            f"  Счёт #{inv['number']} (id={inv['id']})"
            f"  сумма={total:,.0f} ₽  оплачено={paid:,.0f} ₽  [{status}]"
        )
        print(f"  Плательщик: {inv.get('payer_name', '—')}  |  {inv.get('create_date', '')}")
        svcs = inv.get("services", [])
        if svcs:
            print(f"  {'Услуга':<55} {'Кол-во':>7} {'Сумма':>10}")
            print(f"  {'─'*75}")
            for line in svcs:
                name = line.get("name", "")[:54]
                print(f"  {name:<55} {line.get('count', 0):>7} {line.get('amount', 0):>10,.0f}")
        print()

# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Bnovo API — бронирования, доп. услуги, счета")
    parser.add_argument("--from", dest="date_from", help="Дата начала (YYYY-MM-DD), по умолчанию -14 дней")
    parser.add_argument("--to", dest="date_to", help="Дата конца (YYYY-MM-DD), по умолчанию сегодня")
    parser.add_argument("--type", dest="data_type", choices=list(filter(None, DATA_TYPES)), help="Тип выборки")
    parser.add_argument("--json", dest="raw_json", action="store_true", help="Вывести сырой JSON")
    parser.add_argument("--id", dest="user_id", type=int, help="ID пользователя API")
    parser.add_argument("--password", dest="password", help="API ключ")
    parser.add_argument("--services", action="store_true", help="Каталог доп. услуг отеля")
    parser.add_argument("--booking-services", dest="booking_services_id", type=int, metavar="BOOKING_ID",
                        help="Доп. услуги конкретного бронирования")
    parser.add_argument("--booking-invoices", dest="booking_invoices_id", type=int, metavar="BOOKING_ID",
                        help="Счета конкретного бронирования")
    args = parser.parse_args()

    # Учётные данные: приоритет — аргументы → переменные окружения → кэшированный user_id.
    user_id = args.user_id or int(os.environ.get("BNOVO_ID", 0))
    password = args.password or os.environ.get("BNOVO_PASSWORD", "")

    if not user_id:
        user_id = _load_cached_user_id()

    if not user_id or not password:
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

    def with_401_retry(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except RuntimeError as e:
            if "401" in str(e) and user_id and password:
                print("Токен отклонён (401), обновляем...", file=sys.stderr)
                nonlocal token
                token = _authenticate(user_id, password)
                return fn(*a, **kw)
            raise

    # ── Доп. услуги каталог ──────────────────────────────────────────────────
    if args.services:
        services = with_401_retry(get_services, token)
        if args.raw_json:
            print(json.dumps(services, ensure_ascii=False, indent=2))
        else:
            print_services(services)
        return

    # ── Доп. услуги по брони ─────────────────────────────────────────────────
    if args.booking_services_id is not None:
        svcs = with_401_retry(get_booking_services, token, args.booking_services_id)
        if args.raw_json:
            print(json.dumps(svcs, ensure_ascii=False, indent=2))
        else:
            print_booking_services(svcs, args.booking_services_id)
        return

    # ── Счета по брони ───────────────────────────────────────────────────────
    if args.booking_invoices_id is not None:
        invoices = with_401_retry(get_booking_invoices, token, args.booking_invoices_id)
        if args.raw_json:
            print(json.dumps(invoices, ensure_ascii=False, indent=2))
        else:
            print_booking_invoices(invoices, args.booking_invoices_id)
        return

    # ── Бронирования (по умолчанию) ──────────────────────────────────────────
    today = date.today()
    date_to = args.date_to or today.isoformat()
    date_from = args.date_from or (today - timedelta(days=14)).isoformat()

    bookings = with_401_retry(get_bookings, token, date_from, date_to, args.data_type)

    if args.raw_json:
        print(json.dumps(bookings, ensure_ascii=False, indent=2))
    else:
        print_report(bookings, date_from, date_to, args.data_type)


if __name__ == "__main__":
    main()
