from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from threading import Lock

import openpyxl


MONTHS = {1: "JAN", 2: "FEV", 3: "MAR", 4: "ABR", 5: "MAI", 6: "JUN", 7: "JUL", 8: "AGO", 9: "SET", 10: "OUT", 11: "NOV", 12: "DEZ"}
_file_lock = Lock()


def normalize(value: object) -> str:
    import unicodedata

    text = "" if value is None else str(value).strip().upper()
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.sub(r"[^A-Z0-9]+", " ", text).strip()


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", normalize(value).lower()).strip("-")


def display_base(product: object, color: object) -> str:
    p = normalize(product)
    c = normalize(color)
    if p == "CAMISETA":
        return {"BRANCO": "Camiseta Branca", "PRETO": "Camiseta Preta", "AMARELO": "Camiseta Amarela"}.get(c, "Camiseta")
    if p == "CORTA VENTO":
        return "Corta-Vento Dupla Face"
    return str(product or "Produto").strip().title()


def current_sheet_name(now: datetime | None = None) -> str:
    return MONTHS[(now or datetime.now()).month]


def read_stock_rows(path: Path, now: datetime | None = None) -> list[dict]:
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheet_name = current_sheet_name(now)
    if sheet_name not in workbook.sheetnames:
        sheet_name = workbook.sheetnames[0]
    sheet = workbook[sheet_name]
    rows: list[dict] = []
    for row_number, row in enumerate(sheet.iter_rows(min_row=2, max_col=15, values_only=True), start=2):
        product, color, size = row[0], row[1], row[2]
        if not product:
            continue
        base = display_base(product, color)
        quantity = row[14]
        try:
            quantity = max(0, int(float(quantity or 0)))
        except (TypeError, ValueError):
            quantity = 0
        size_text = str(size or "Único").strip()
        name = f"{base} - {size_text}" if size_text.lower() != "único" else base
        rows.append({
            "source_key": f"{sheet_name}:{row_number}:{normalize(product)}:{normalize(color)}:{normalize(size)}",
            "sku": f"KLUBI-{slug(base)}-{slug(size_text) or 'unico'}-{row_number}",
            "name": name,
            "base_name": base,
            "color": str(color or "").strip(),
            "size": size_text,
            "quantity": quantity,
            "source_sheet": sheet_name,
        })
    workbook.close()
    return rows


def read_current_stock_rows(path: Path) -> list[dict]:
    """Lê o saldo físico consolidado das abas de Eugênio e Grand Station."""
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    selected = []
    for sheet_name in workbook.sheetnames:
        key = normalize(sheet_name)
        if "EUGENIO" in key or "GRAND" in key or "GRAND STATION" in key or "G S" in key:
            selected.append((sheet_name, "EUGENIO" if "EUGENIO" in key else "GRAND_STATION"))
    if not selected:
        workbook.close()
        raise ValueError("Não encontrei as abas de estoque de Eugênio ou Grand Station.")

    totals: dict[tuple[str, str], dict] = {}
    for sheet_name, location in selected:
        sheet = workbook[sheet_name]
        headers = [normalize(sheet.cell(2, column).value) for column in range(1, 8)]
        if not headers or "PRODUTOS" not in headers[0]:
            continue
        sizes = [str(sheet.cell(2, column).value or "").strip() for column in range(3, 8)]
        for row_number, row in enumerate(sheet.iter_rows(min_row=3, max_col=7, values_only=True), start=3):
            label = str(row[0] or "").strip()
            if not label:
                continue
            normalized_label = normalize(label)
            if normalized_label.startswith("CAMISETA "):
                camiseta_color = {"BRANCA": "BRANCO", "PRETA": "PRETO", "AMARELA": "AMARELO"}.get(normalized_label.replace("CAMISETA ", "", 1), normalized_label.replace("CAMISETA ", "", 1))
                base_name = display_base("CAMISETA", camiseta_color)
            elif normalized_label in {"CORTA VENTO", "CORTA VENTO DUPLA FACE"}:
                base_name = "Corta-Vento Dupla Face"
            elif normalized_label == "MOLETOM":
                base_name = "Moletom"
            else:
                base_name = label.strip().title()

            size_values = list(row[2:7])
            has_size_values = any(value not in (None, "", "-") for value in size_values)
            if has_size_values:
                for size, value in zip(sizes, size_values):
                    try:
                        quantity = max(0, int(float(value or 0)))
                    except (TypeError, ValueError):
                        quantity = 0
                    key = (normalize(base_name), normalize(size))
                    item = totals.setdefault(key, {"base_name": base_name, "size": size or "Único", "quantity": 0, "locations": set()})
                    item["quantity"] += quantity
                    item["locations"].add(location)
            else:
                value = row[1]
                if value in (None, "", "-"):
                    continue
                try:
                    quantity = max(0, int(float(value or 0)))
                except (TypeError, ValueError):
                    quantity = 0
                key = (normalize(base_name), "UNICO")
                item = totals.setdefault(key, {"base_name": base_name, "size": "Único", "quantity": 0, "locations": set()})
                item["quantity"] += quantity
                item["locations"].add(location)
    workbook.close()
    for item in totals.values():
        item["locations"] = sorted(item["locations"])
    return list(totals.values())


def read_price_hints(path: Path) -> dict[str, int]:
    """Lê preços já registrados na planilha histórica para preencher o catálogo inicial."""
    if not path.exists():
        return {}
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    hints: dict[str, list[int]] = {}
    pattern = re.compile(r"R\$\s*([0-9]+(?:[.,][0-9]{1,2})?)", re.IGNORECASE)
    names = ["garrafa", "caneca", "agenda", "ecobag", "meia", "camiseta branca", "camiseta preta", "camiseta amarela", "moletom branco", "moletom preto", "corta-vento", "mochila"]
    for sheet in workbook.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                text = str(cell.value or "")
                match = pattern.search(text)
                if not match:
                    continue
                raw = match.group(1).replace(".", "").replace(",", ".")
                try:
                    cents = int(round(float(raw) * 100))
                except ValueError:
                    continue
                normalized = normalize(text)
                for name in names:
                    if normalize(name) in normalized:
                        hints.setdefault(normalize(name), []).append(cents)
    workbook.close()
    return {key: sorted(values)[len(values) // 2] for key, values in hints.items() if values}


def _write_atomically(path: Path, mutate) -> None:
    with _file_lock:
        workbook = openpyxl.load_workbook(path)
        mutate(workbook)
        fd, temporary = tempfile.mkstemp(prefix=f"{path.stem}-", suffix=".xlsx", dir=path.parent)
        os.close(fd)
        try:
            workbook.save(temporary)
            os.replace(temporary, path)
        finally:
            workbook.close()
            if os.path.exists(temporary):
                os.unlink(temporary)


def record_completed_order(path: Path, order: dict, items: list[dict], competence: str) -> None:
    """Registra o pedido no resumo mensal e em uma aba detalhada, sem duplicação."""
    month_number = int(competence.split("-")[1]) if "-" in competence and competence.split("-")[1].isdigit() else datetime.now().month
    month = MONTHS.get(month_number, current_sheet_name())
    order_number = str(order.get("order_number") or order.get("id") or "")
    name = order.get("customer_name") or order.get("customer_email") or order.get("customer_identifier")
    email = order.get("customer_email") or order.get("customer_identifier") or ""
    status = "Entregue" if order.get("status") == "CONCLUIDO" else "Aprovado" if order.get("status") == "APROVADO" else "Pendente"
    timestamp = order.get("source_created_at") or order.get("confirmed_at") or order.get("created_at") or ""

    def cents(item: dict, field: str) -> int:
        if field in item and item[field] is not None:
            return int(item[field])
        key = "unit_price" if field == "unit_price_cents" else "total_price"
        return int(round(float(item.get(key) or 0) * 100))

    with _file_lock:
        workbook = openpyxl.load_workbook(path)
        numeric_month = f"{month_number:02d}"
        if numeric_month in workbook.sheetnames and numeric_month != month:
            workbook.remove(workbook[numeric_month])
        sheet = workbook[month] if month in workbook.sheetnames else workbook.create_sheet(month)
        headers = ["Nome do colaborador", "ITEM", "QUANTIDADE", "VALOR", "STATUS", "DESCONTO", "PEDIDOS"]
        for col, value in enumerate(headers, 1):
            if sheet.cell(1, col).value is None:
                sheet.cell(1, col).value = value
        existing_row = next((row_number for row_number in range(2, sheet.max_row + 1) if normalize(sheet.cell(row_number, 1).value) == normalize(name)), None)
        if existing_row is None:
            existing_row = sheet.max_row + 1
        registered_orders = {value.strip() for value in str(sheet.cell(existing_row, 7).value or "").split(",") if value.strip()}
        already_registered = order_number in registered_orders
        if order.get("status") == "PENDENTE":
            already_registered = True
        old_items = str(sheet.cell(existing_row, 2).value or "").strip()
        item_texts = [f"{item['product_name_snapshot']} (R$ {cents(item, 'unit_price_cents') / 100:.2f})" for item in items]
        if order.get("status") == "PENDENTE":
            for item_text in item_texts:
                old_items = old_items.replace(item_text, "").replace(", ,", ",").strip(" ,")
            old_quantity = int(float(sheet.cell(existing_row, 3).value or 0)) if sheet.cell(existing_row, 3).value else 0
            old_total = float(sheet.cell(existing_row, 4).value or 0) if sheet.cell(existing_row, 4).value else 0
            sheet.cell(existing_row, 3).value = max(0, old_quantity - sum(int(item["quantity"]) for item in items))
            sheet.cell(existing_row, 4).value = max(0, old_total - sum(cents(item, "total_price_cents") for item in items) / 100)
            registered_orders.discard(order_number)
        elif not already_registered:
            sheet.cell(existing_row, 3).value = (int(float(sheet.cell(existing_row, 3).value or 0)) if sheet.cell(existing_row, 3).value else 0) + sum(int(item["quantity"]) for item in items)
            sheet.cell(existing_row, 4).value = (float(sheet.cell(existing_row, 4).value or 0) if sheet.cell(existing_row, 4).value else 0) + sum(cents(item, "total_price_cents") for item in items) / 100
            old_items = ", ".join(filter(None, [old_items, ", ".join(item_texts)]))
            registered_orders.add(order_number)
        sheet.cell(existing_row, 1).value = name
        sheet.cell(existing_row, 2).value = old_items
        sheet.cell(existing_row, 5).value = status
        sheet.cell(existing_row, 6).value = "Não"
        sheet.cell(existing_row, 7).value = ", ".join(sorted(registered_orders))

        detail = workbook["DETALHES"] if "DETALHES" in workbook.sheetnames else workbook.create_sheet("DETALHES")
        detail_headers = ["PEDIDO", "DATA/HORA", "NOME", "E-MAIL", "PRODUTO", "QUANTIDADE", "TAMANHO", "VALOR UNITÁRIO", "VALOR TOTAL", "STATUS"]
        for col, value in enumerate(detail_headers, 1):
            detail.cell(1, col).value = value
        if order.get("status") == "PENDENTE":
            for row_number in range(detail.max_row, 1, -1):
                if str(detail.cell(row_number, 1).value or "") == order_number:
                    detail.delete_rows(row_number, 1)
        else:
            for item in items:
                product_name = str(item.get("product_name_snapshot") or item.get("product") or "")
                size = product_name.rsplit(" - ", 1)[1] if " - " in product_name else "Único"
                detail_row = next((row_number for row_number in range(2, detail.max_row + 1) if str(detail.cell(row_number, 1).value or "") == order_number and str(detail.cell(row_number, 5).value or "") == product_name), None)
                if detail_row is None:
                    detail_row = detail.max_row + 1
                values = [order_number, timestamp, name, email, product_name, int(item["quantity"]), size, cents(item, "unit_price_cents") / 100, cents(item, "total_price_cents") / 100, status]
                for col, value in enumerate(values, 1):
                    detail.cell(detail_row, col).value = value

        fd, temporary = tempfile.mkstemp(prefix=f"{path.stem}-", suffix=".xlsx", dir=path.parent)
        os.close(fd)
        try:
            workbook.save(temporary)
            os.replace(temporary, path)
        finally:
            workbook.close()
            if os.path.exists(temporary):
                os.unlink(temporary)
