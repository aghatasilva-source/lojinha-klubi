from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build

from .excel_files import normalize


SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


def _header_map(headers: list[object]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for index, header in enumerate(headers):
        key = normalize(header)
        if key:
            mapping[key] = index
    return mapping


def parse_product_and_price(value: str) -> tuple[str, int | None]:
    """Converte o valor do formulário no formato Produto | 23,50."""
    parts = re.split(r"\s*\|\s*", value or "", maxsplit=1)
    product = parts[0].strip()
    if len(parts) == 1:
        return product, None
    raw_price = re.sub(r"[^0-9,.]", "", parts[1]).strip()
    if not raw_price:
        return product, None
    try:
        normalized = raw_price.replace(".", "").replace(",", ".") if "," in raw_price else raw_price
        return product, int(round(float(normalized) * 100))
    except ValueError:
        return product, None


def read_order_rows(credentials_file: Path, spreadsheet_id: str, sheet_name: str) -> list[dict]:
    credentials = service_account.Credentials.from_service_account_file(credentials_file, scopes=SCOPES)
    service = build("sheets", "v4", credentials=credentials, cache_discovery=False)
    response = service.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{sheet_name}'!A:Z",
        majorDimension="ROWS",
    ).execute()
    values = response.get("values", [])
    if not values:
        return []
    headers = _header_map(values[0])

    def value(row: list[object], *names: str) -> str:
        for name in names:
            normalized_name = normalize(name)
            index = headers.get(normalized_name)
            if index is None:
                index = next((candidate for header, candidate in headers.items() if header.startswith(normalized_name) or normalized_name in header), None)
            if index is not None and index < len(row):
                return str(row[index] or "").strip()
        return ""

    parsed = []
    for row_number, row in enumerate(values[1:], start=2):
        if not any(str(cell).strip() for cell in row):
            continue
        raw_product = value(row, "PRODUTOS", "Produto", "Item")
        product, product_price_cents = parse_product_and_price(raw_product)
        parsed.append({
            "row_number": row_number,
            "timestamp": value(row, "Carimbo de data/hora", "Data", "Data e horário"),
            "source_created_at": parse_timestamp(value(row, "Carimbo de data/hora", "Data", "Data e horário")),
            "email": value(row, "Endereço de e-mail", "E-mail", "Email"),
            "name": value(row, "Nome", "Nome do colaborador", "Colaborador"),
            "product": product,
            "product_source": raw_product,
            "unit_price_cents": product_price_cents,
            "quantity": value(row, "QUANTIDADE", "Quantidade"),
            "size": value(row, "TAMANHO", "Tamanho"),
        })
    return parsed


def parse_timestamp(value: str) -> str | None:
    for pattern in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(value.strip(), pattern).replace(tzinfo=timezone(timedelta(hours=-3))).isoformat()
        except ValueError:
            continue


def split_products(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,;\n]+", value or "") if part.strip()]
