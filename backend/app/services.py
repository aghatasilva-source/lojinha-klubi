from __future__ import annotations

import json
import re
import uuid
import csv
import io
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import settings
from .db import connect, transaction, utc_now
from .errors import (
    DuplicateOrderError,
    InsufficientStockError,
    InvalidOrderStatusError,
    OrderNotFoundError,
    ProductNotFoundError,
    UnauthorizedOperationError,
)
from .integrations.excel_files import normalize, read_current_stock_rows, read_price_hints, read_stock_rows, record_completed_order
from .integrations.google_sheets import read_order_rows, split_products


_log_lock = threading.Lock()


def _format_log_message(event: str, details: dict[str, Any]) -> str:
    if event == "AUDITORIA":
        action = details.get("action", "").upper()
        order_id = details.get("order_number") or details.get("entity_id", "")
        labels = {
            "APROVADO": f"Aprovei o pedido {order_id} e baixei o estoque.",
            "CANCELADO": f"Cancelei o pedido {order_id}.",
            "CONCLUIDO": f"Concluí o pedido {order_id}.",
            "DESFAZIMENTO_CANCELAMENTO": f"Desfiz o cancelamento do pedido {order_id}.",
            "DESFAZIMENTO_APROVACAO": f"Desfiz a aprovação do pedido {order_id} e devolvi os itens ao estoque.",
            "DESFAZIMENTO_CONCLUSAO": f"Desfiz a conclusão do pedido {order_id}; ele voltou para Aprovado.",
            "ENTRADA": f"Comprei e adicionei {details.get('quantity', '?')} unidade(s) de {details.get('product_name', 'produto')} ao estoque.",
            "AJUSTE_MANUAL": f"Ajustei o estoque de {details.get('product_name', 'produto')}.",
            "RECONCILIACAO": f"Atualizei {details.get('product_name', 'produto')} de {details.get('old_quantity', '?')} para {details.get('new_quantity', '?')} unidade(s).",
            "DEVOLUCAO": f"Registrei uma devolução do pedido {order_id}.",
            "SAIDA_NAO_COMERCIAL": f"Retirei {details.get('items_summary', 'itens')} para {details.get('reason_label', 'saída não comercial')} de {details.get('recipient', 'destinatário não informado')}.",
        }
        return labels.get(action, f"Registrei a ação {action or 'operacional'} no pedido {order_id}.")
    if event == "INTERACAO_USUARIO":
        action = details.get("action", "").upper()
        extra = details.get("details") or {}
        if action == "CLIQUE_INTERFACE":
            return f"Cliquei em {extra.get('label') or 'um botão'}."
        if action == "EDICAO_CAMPO":
            return f"Editei o campo {extra.get('name') or 'sem identificação'}."
        if action == "DESFAZER_PEDIDO":
            return f"Desfiz o pedido {extra.get('order_id', '?')}."
        return f"Executei a ação {action or 'não identificada'}."
    if event == "SINCRONIZACAO_GOOGLE_SHEETS":
        return f"Sincronizei os pedidos: {details.get('importados', 0)} novo(s), {details.get('ignorados', 0)} já existente(s)."
    if event == "NOTIFICACAO":
        return f"Sistema: {details.get('title', 'notificação')} — {details.get('message', '')}".rstrip(" —")
    return f"{event}: {details.get('message') or details.get('action') or 'ação registrada'}."


def append_log(event: str, details: dict[str, Any] | None = None) -> None:
    """Registra uma linha curta e legível no LOG operacional."""
    details = details or {}
    timestamp = datetime.now().strftime("%d/%m/%Y %H:%M")
    log_path = settings.root / "LOG"
    with _log_lock:
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(f"{timestamp} — {_format_log_message(event, details)}\n")


def money(cents: int) -> float:
    return round(int(cents or 0) / 100, 2)


def row_dict(row) -> dict[str, Any]:
    return dict(row) if row else {}


def serialize_order(connection, order_id: int) -> dict:
    order = row_dict(connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())
    items = [row_dict(row) for row in connection.execute("SELECT * FROM order_items WHERE order_id = ? ORDER BY id", (order_id,))]
    for item in items:
        item["unit_price"] = money(item.pop("unit_price_cents"))
        item["total_price"] = money(item.pop("total_price_cents"))
    order["subtotal"] = money(order.pop("subtotal_cents", 0))
    order["total"] = money(order.pop("total_cents", 0))
    order["items"] = items
    return order


def _audit(connection, entity_type: str, entity_id: str, action: str, old: Any, new: Any, user_id: int = 1, request_id: str | None = None, write_log: bool = True) -> None:
    connection.execute(
        "INSERT INTO audit_log(entity_type,entity_id,action,old_value,new_value,user_id,request_id,created_at) VALUES (?,?,?,?,?,?,?,?)",
        (entity_type, entity_id, action, json.dumps(old, ensure_ascii=False, default=str) if old is not None else None,
         json.dumps(new, ensure_ascii=False, default=str) if new is not None else None, user_id, request_id or str(uuid.uuid4()), utc_now()),
    )
    audit_details = {"entity_type": entity_type, "entity_id": entity_id, "action": action, "user_id": user_id}
    if entity_type == "ORDER":
        order_row = connection.execute("SELECT order_number FROM orders WHERE id=?", (entity_id,)).fetchone()
        if order_row:
            audit_details["order_number"] = order_row["order_number"]
    if isinstance(old, dict):
        audit_details["quantity"] = new.get("added_quantity") if isinstance(new, dict) and new.get("added_quantity") is not None else new.get("quantity_after") if isinstance(new, dict) else None
    if isinstance(new, dict):
        audit_details.update({key: new[key] for key in ("product_name", "added_quantity", "old_quantity", "new_quantity", "locations", "items_summary", "reason_label", "recipient") if key in new})
    if write_log:
        append_log("AUDITORIA", audit_details)


def _notification(connection, kind: str, title: str, message: str, entity_type: str | None = None, entity_id: str | None = None) -> None:
    connection.execute(
        "INSERT INTO notifications(kind,title,message,entity_type,entity_id,created_at) VALUES (?,?,?,?,?,?)",
        (kind, title, message, entity_type, entity_id, utc_now()),
    )
    append_log("NOTIFICACAO", {"kind": kind, "title": title, "message": message, "entity_type": entity_type, "entity_id": entity_id})


def _idempotent_result(connection, key: str | None, operation: str) -> dict | None:
    if not key:
        return None
    row = connection.execute("SELECT result_json FROM idempotency_keys WHERE key = ? AND operation_type = ?", (key, operation)).fetchone()
    return json.loads(row[0]) if row else None


def _save_idempotent(connection, key: str | None, operation: str, result: dict) -> None:
    if key:
        connection.execute("INSERT INTO idempotency_keys(key,operation_type,result_json,created_at) VALUES (?,?,?,?)", (key, operation, json.dumps(result, ensure_ascii=False), utc_now()))


def seed_catalog_from_xlsx() -> int:
    if not settings.estoque_xlsx.exists():
        return 0
    rows = read_stock_rows(settings.estoque_xlsx)
    now = utc_now()
    with transaction(immediate=True) as connection:
        has_products = connection.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        if has_products:
            return 0
        for item in rows:
            cursor = connection.execute(
                "INSERT INTO products(sku,name,base_name,color,size,source_key,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (item["sku"], item["name"], item["base_name"], item["color"], item["size"], item["source_key"], now, now),
            )
            product_id = cursor.lastrowid
            connection.execute("INSERT INTO inventory(product_id,physical_quantity,updated_at) VALUES (?,?,?)", (product_id, item["quantity"], now))
            if item["quantity"]:
                connection.execute(
                    "INSERT INTO stock_movements(product_id,movement_type,quantity,quantity_before,quantity_after,reserved_before,reserved_after,reference_type,reason,user_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (product_id, "ENTRADA", item["quantity"], 0, item["quantity"], 0, 0, "IMPORTACAO", "Saldo inicial importado do arquivo de estoque", 1, now),
                )
    return len(rows)


def hydrate_prices_from_history() -> int:
    hints = read_price_hints(settings.pedidos_concluidos_xlsx)
    if not hints:
        return 0
    changed = 0
    with transaction(immediate=True) as connection:
        products = connection.execute("SELECT id,base_name,unit_price_cents FROM products").fetchall()
        for product in products:
            price = hints.get(normalize(product["base_name"]))
            if price is None:
                price = next((value for key, value in hints.items() if key in normalize(product["base_name"]) or normalize(product["base_name"]) in key), None)
            if price and not product["unit_price_cents"]:
                connection.execute("UPDATE products SET unit_price_cents=?,updated_at=? WHERE id=?", (price, utc_now(), product["id"]))
                connection.execute("UPDATE order_items SET unit_price_cents=?,total_price_cents=quantity*? WHERE product_id=? AND unit_price_cents=0", (price, price, product["id"]))
                changed += 1
        connection.execute("UPDATE orders SET subtotal_cents=(SELECT COALESCE(SUM(total_price_cents),0) FROM order_items WHERE order_id=orders.id), total_cents=(SELECT COALESCE(SUM(total_price_cents),0) FROM order_items WHERE order_id=orders.id) WHERE status='PENDENTE'")
    return changed


def _find_product(connection, product_text: str, size: str | None = None):
    normalized = normalize(product_text)
    candidates = connection.execute("SELECT * FROM products WHERE status = 'ATIVO'").fetchall()
    exact = []
    for row in candidates:
        base = normalize(row["base_name"])
        name = normalize(row["name"])
        if normalized == base or normalized == name or normalized in name or name in normalized or base in normalized:
            exact.append(row)
    if size:
        sized = [row for row in exact if normalize(row["size"]) == normalize(size)]
        if sized:
            return sized[0]
    return exact[0] if exact else None


def sync_google_orders() -> dict:
    if not settings.google_credentials_file.exists():
        return {"importados": 0, "ignorados": 0, "erro": "Credencial do Google não encontrada."}
    rows = read_order_rows(settings.google_credentials_file, settings.spreadsheet_id, settings.sheet_name)
    imported = 0
    ignored = 0
    errors: list[str] = []
    with transaction(immediate=True) as connection:
        for row in rows:
            source_key = f"sheets:{settings.spreadsheet_id}:{settings.sheet_gid}:{row['row_number']}"
            existing = connection.execute("SELECT id,source_created_at,status FROM orders WHERE source_key = ?", (source_key,)).fetchone()
            if existing:
                if not existing["source_created_at"] and row.get("source_created_at"):
                    connection.execute("UPDATE orders SET source_created_at=? WHERE id=?", (row["source_created_at"], existing["id"]))
                if existing["status"] == "PENDENTE" and row.get("unit_price_cents") is not None:
                    item = connection.execute("SELECT id,product_id FROM order_items WHERE order_id=?", (existing["id"],)).fetchone()
                    if item:
                        price = int(row["unit_price_cents"])
                        item_name = f"{row['product']} - {row['size']}" if row.get("size") else row["product"]
                        connection.execute("UPDATE order_items SET product_name_snapshot=?,unit_price_cents=?,total_price_cents=quantity*? WHERE id=?", (item_name, price, price, item["id"]))
                        connection.execute("UPDATE orders SET subtotal_cents=(SELECT COALESCE(SUM(total_price_cents),0) FROM order_items WHERE order_id=?),total_cents=(SELECT COALESCE(SUM(total_price_cents),0) FROM order_items WHERE order_id=?),updated_at=? WHERE id=?", (existing["id"], existing["id"], utc_now(), existing["id"]))
                        connection.execute("UPDATE products SET unit_price_cents=?,updated_at=? WHERE id=?", (price, utc_now(), item["product_id"]))
                ignored += 1
                continue
            try:
                quantity = int(float(str(row["quantity"]).replace(",", ".")))
            except (TypeError, ValueError):
                errors.append(f"Linha {row['row_number']}: quantidade inválida")
                continue
            if quantity <= 0 or not row["product"]:
                errors.append(f"Linha {row['row_number']}: produto ou quantidade ausente")
                continue
            products = split_products(row["product"])
            product = _find_product(connection, products[0], row["size"])
            if not product:
                errors.append(f"Linha {row['row_number']}: produto não cadastrado ({row['product']})")
                continue
            customer_email = row["email"] or row["name"] or "sem-identificador@klubi.local"
            customer_name = row["name"] or (row["email"].split("@")[0] if row["email"] else "Colaborador não informado")
            order_number = f"KLUBI-{datetime.now().year}-{row['row_number']:05d}"
            if connection.execute("SELECT 1 FROM orders WHERE order_number = ?", (order_number,)).fetchone():
                order_number = f"{order_number}-{uuid.uuid4().hex[:6].upper()}"
            now = utc_now()
            cursor = connection.execute(
                "INSERT INTO orders(order_number,customer_identifier,customer_name,customer_email,status,source_key,source_created_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (order_number, customer_email, customer_name, row["email"] or None, "PENDENTE", source_key, row.get("source_created_at"), now, now),
            )
            order_id = cursor.lastrowid
            unit_price = int(row["unit_price_cents"] if row.get("unit_price_cents") is not None else product["unit_price_cents"] or 0)
            if row.get("unit_price_cents") is not None:
                connection.execute("UPDATE products SET unit_price_cents=?,updated_at=? WHERE id=?", (unit_price, now, product["id"]))
            item_name = f"{row['product']} - {row['size']}" if row.get("size") else row["product"]
            connection.execute(
                "INSERT INTO order_items(order_id,product_id,sku_snapshot,product_name_snapshot,quantity,unit_price_cents,total_price_cents,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (order_id, product["id"], product["sku"], item_name, quantity, unit_price, unit_price * quantity, now),
            )
            connection.execute("UPDATE orders SET subtotal_cents=?,total_cents=? WHERE id=?", (unit_price * quantity, unit_price * quantity, order_id))
            _audit(connection, "ORDER", str(order_id), "CRIADO_POR_SINCRONIZACAO", None, {"source_key": source_key, "order_number": order_number})
            _notification(connection, "NOVO_PEDIDO", "Novo pedido recebido", f"Pedido {order_number} aguardando análise.", "order", str(order_id))
            imported += 1
    result = {"importados": imported, "ignorados": ignored, "erros": errors}
    append_log("SINCRONIZACAO_GOOGLE_SHEETS", result)
    return result


def list_products(page: int = 1, page_size: int = 50, low_only: bool = False) -> dict:
    page_size = min(max(page_size, 1), 100)
    offset = (max(page, 1) - 1) * page_size
    with connect() as connection:
        where = "WHERE p.status = 'ATIVO'" + (" AND i.available_quantity <= CAST((SELECT value FROM settings WHERE key='stock_low_limit') AS INTEGER)" if low_only else "")
        total = connection.execute(f"SELECT COUNT(*) FROM products p JOIN inventory i ON i.product_id=p.id {where}").fetchone()[0]
        rows = connection.execute(f"SELECT p.*, i.physical_quantity, i.reserved_quantity, i.available_quantity, i.updated_at AS inventory_updated_at FROM products p JOIN inventory i ON i.product_id=p.id {where} ORDER BY p.name LIMIT ? OFFSET ?", (page_size, offset)).fetchall()
        data = []
        for row in rows:
            item = row_dict(row)
            item["unit_price"] = money(item.pop("unit_price_cents"))
            quantity = item["available_quantity"]
            item["situation"] = "Sem estoque" if quantity == 0 else "Estoque baixo" if quantity <= settings.stock_low_limit else "Em estoque"
            data.append(item)
        return {"items": data, "page": page, "page_size": page_size, "total_items": total, "total_pages": (total + page_size - 1) // page_size}


def list_store_products() -> dict:
    data = list_products(1, 100, False)["items"]
    return {"items": [{key: item[key] for key in ("id", "name", "base_name", "color", "size", "sku", "unit_price", "available_quantity", "situation")} for item in data]}


def create_store_order(items: list[dict], identity: dict) -> dict:
    if not isinstance(items, list) or not items:
        raise ValueError("Selecione pelo menos um produto.")
    quantities: dict[int, int] = {}
    for item in items:
        try:
            product_id = int(item.get("product_id", 0))
            quantity = int(item.get("quantity", 0))
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("Produto e quantidade são obrigatórios.") from error
        if product_id <= 0 or quantity <= 0:
            raise ValueError("Produto e quantidade devem ser maiores que zero.")
        quantities[product_id] = quantities.get(product_id, 0) + quantity

    with transaction(immediate=True) as connection:
        products = {}
        for product_id, quantity in quantities.items():
            product = connection.execute("SELECT p.*,i.available_quantity FROM products p JOIN inventory i ON i.product_id=p.id WHERE p.id=? AND p.status='ATIVO'", (product_id,)).fetchone()
            if not product:
                raise ProductNotFoundError("Produto não encontrado.")
            if product["available_quantity"] < quantity:
                raise InsufficientStockError(f"Não há quantidade disponível suficiente para {product['name']}.")
            products[product_id] = (product, quantity)

        now = utc_now()
        year = datetime.now().year
        sequence = connection.execute("SELECT COUNT(*) FROM orders WHERE order_number LIKE ?", (f"KLUBI-{year}-%",)).fetchone()[0] + 1
        order_number = f"KLUBI-{year}-{sequence:05d}"
        while connection.execute("SELECT 1 FROM orders WHERE order_number=?", (order_number,)).fetchone():
            sequence += 1
            order_number = f"KLUBI-{year}-{sequence:05d}"
        email = identity["email"]
        name = identity.get("name") or email.split("@")[0]
        source_key = f"portal:{uuid.uuid4().hex}"
        cursor = connection.execute("INSERT INTO orders(order_number,customer_identifier,customer_name,customer_email,status,source_key,source_created_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)", (order_number, email, name, email, "PENDENTE", source_key, now, now, now))
        order_id = cursor.lastrowid
        total = 0
        for product, quantity in products.values():
            unit_price = int(product["unit_price_cents"] or 0)
            item_total = unit_price * quantity
            total += item_total
            connection.execute("INSERT INTO order_items(order_id,product_id,sku_snapshot,product_name_snapshot,quantity,unit_price_cents,total_price_cents,created_at) VALUES (?,?,?,?,?,?,?,?)", (order_id, product["id"], product["sku"], product["name"], quantity, unit_price, item_total, now))
        connection.execute("UPDATE orders SET subtotal_cents=?,total_cents=? WHERE id=?", (total, total, order_id))
        _audit(connection, "ORDER", str(order_id), "CRIADO_PEDIDO_ONLINE", None, {"order_number": order_number, "email": email, "source_key": source_key}, 1)
        _notification(connection, "NOVO_PEDIDO", "Novo pedido recebido", f"Pedido {order_number} aguardando análise.", "order", str(order_id))
        return serialize_order(connection, order_id)


def list_store_orders(email: str) -> list[dict]:
    with connect() as connection:
        rows = connection.execute("SELECT id FROM orders WHERE customer_email=? AND source_key LIKE 'portal:%' ORDER BY COALESCE(source_created_at,created_at) DESC", (email,)).fetchall()
        return [serialize_order(connection, row["id"]) for row in rows]


def list_orders(status: str | None = None, search: str | None = None, page: int = 1, page_size: int = 50) -> dict:
    page_size = min(max(page_size, 1), 100)
    offset = (max(page, 1) - 1) * page_size
    with connect() as connection:
        clauses, args = [], []
        if status and status != "TODOS":
            clauses.append("o.status = ?"); args.append(status)
        if search:
            clauses.append("(o.order_number LIKE ? OR o.customer_name LIKE ? OR o.customer_identifier LIKE ? OR EXISTS (SELECT 1 FROM order_items x JOIN products y ON y.id=x.product_id WHERE x.order_id=o.id AND y.name LIKE ?))")
            needle = f"%{search}%"; args.extend([needle, needle, needle, needle])
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        total = connection.execute(f"SELECT COUNT(*) FROM orders o{where}", args).fetchone()[0]
        rows = connection.execute(f"SELECT o.*, (SELECT COUNT(*) FROM order_items i WHERE i.order_id=o.id) AS item_count FROM orders o{where} ORDER BY COALESCE(o.source_created_at, o.created_at) DESC LIMIT ? OFFSET ?", [*args, page_size, offset]).fetchall()
        data = []
        for row in rows:
            item = row_dict(row); item["total"] = money(item.pop("total_cents")); item["subtotal"] = money(item.pop("subtotal_cents")); data.append(item)
            item_rows = connection.execute("SELECT product_name_snapshot,quantity,unit_price_cents FROM order_items WHERE order_id=? ORDER BY id", (item["id"],)).fetchall()
            item["items"] = [{"product": child["product_name_snapshot"], "quantity": child["quantity"], "unit_price": money(child["unit_price_cents"])} for child in item_rows]
            item["products"] = ", ".join((child["product_name_snapshot"].rsplit(" - ", 1)[0] if " - " in child["product_name_snapshot"] else child["product_name_snapshot"]) for child in item_rows)
            item["quantities"] = ", ".join(str(child["quantity"]) for child in item_rows)
            item["sizes"] = ", ".join((child["product_name_snapshot"].split(" - ")[-1] if " - " in child["product_name_snapshot"] else "Único") for child in item_rows)
        return {"items": data, "page": page, "page_size": page_size, "total_items": total, "total_pages": (total + page_size - 1) // page_size}


def get_order(order_id: int) -> dict:
    with connect() as connection:
        if not connection.execute("SELECT 1 FROM orders WHERE id = ?", (order_id,)).fetchone():
            raise OrderNotFoundError("Pedido não encontrado.")
        return serialize_order(connection, order_id)


def approve_order(order_id: int, user_id: int = 1, idempotency_key: str | None = None) -> dict:
    with transaction(immediate=True) as connection:
        previous = _idempotent_result(connection, idempotency_key, "APROVAR_PEDIDO")
        if previous:
            return previous
        order = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not order:
            raise OrderNotFoundError("Pedido não encontrado.")
        if order["status"] != "PENDENTE":
            if order["status"] == "APROVADO":
                return serialize_order(connection, order_id)
            raise InvalidOrderStatusError("Somente pedidos pendentes podem ser aprovados.")
        items = connection.execute("SELECT oi.*, p.name FROM order_items oi JOIN products p ON p.id=oi.product_id WHERE oi.order_id=?", (order_id,)).fetchall()
        for item in items:
            inventory = connection.execute("SELECT * FROM inventory WHERE product_id = ?", (item["product_id"],)).fetchone()
            if not inventory or inventory["available_quantity"] < item["quantity"]:
                raise InsufficientStockError(f"Estoque insuficiente para {item['name']}.")
        now = utc_now()
        for item in items:
            inventory = connection.execute("SELECT * FROM inventory WHERE product_id = ?", (item["product_id"],)).fetchone()
            quantity = item["quantity"]
            updated = connection.execute("UPDATE inventory SET reserved_quantity=reserved_quantity+?, version=version+1, updated_at=? WHERE product_id=? AND physical_quantity-reserved_quantity>=?", (quantity, now, item["product_id"], quantity))
            if updated.rowcount != 1:
                raise InsufficientStockError(f"Conflito de estoque para {item['name']}.")
            connection.execute("INSERT INTO stock_movements(product_id,movement_type,quantity,quantity_before,quantity_after,reserved_before,reserved_after,reference_type,reference_id,order_id,reason,user_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (item["product_id"], "RESERVA", quantity, inventory["physical_quantity"], inventory["physical_quantity"], inventory["reserved_quantity"], inventory["reserved_quantity"] + quantity, "PEDIDO", str(order_id), order_id, "Reserva atômica para aprovação do pedido", user_id, now))
            connection.execute("UPDATE inventory SET physical_quantity=physical_quantity-?, reserved_quantity=reserved_quantity-?, version=version+1, updated_at=? WHERE product_id=?", (quantity, quantity, now, item["product_id"]))
            connection.execute("INSERT INTO stock_movements(product_id,movement_type,quantity,quantity_before,quantity_after,reserved_before,reserved_after,reference_type,reference_id,order_id,reason,user_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (item["product_id"], "BAIXA_PEDIDO", quantity, inventory["physical_quantity"], inventory["physical_quantity"] - quantity, inventory["reserved_quantity"] + quantity, inventory["reserved_quantity"], "PEDIDO", str(order_id), order_id, "Baixa do estoque na aprovação do pedido", user_id, now))
            fresh = connection.execute("SELECT available_quantity FROM inventory WHERE product_id=?", (item["product_id"],)).fetchone()[0]
            if fresh <= settings.stock_low_limit:
                _notification(connection, "ESTOQUE_BAIXO", "Produto com estoque baixo", f"{item['name']} está com {fresh} unidade(s).", "product", str(item["product_id"]))
        connection.execute("UPDATE orders SET status='APROVADO', confirmed_at=?, updated_at=? WHERE id=?", (now, now, order_id))
        _audit(connection, "ORDER", str(order_id), "APROVADO", {"status": order["status"]}, {"status": "APROVADO"}, user_id)
        result = serialize_order(connection, order_id)
        _save_idempotent(connection, idempotency_key, "APROVAR_PEDIDO", result)
    if settings.pedidos_concluidos_xlsx.exists():
        record_completed_order(settings.pedidos_concluidos_xlsx, result, result["items"], datetime.now().strftime("%Y-%m"))
    return result


def cancel_order(order_id: int, user_id: int = 1, idempotency_key: str | None = None) -> dict:
    with transaction(immediate=True) as connection:
        previous = _idempotent_result(connection, idempotency_key, "CANCELAR_PEDIDO")
        if previous: return previous
        order = connection.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order: raise OrderNotFoundError("Pedido não encontrado.")
        if order["status"] != "PENDENTE": raise InvalidOrderStatusError("Somente pedidos pendentes podem ser cancelados.")
        now = utc_now(); connection.execute("UPDATE orders SET status='CANCELADO',cancelled_at=?,updated_at=? WHERE id=?", (now, now, order_id))
        _audit(connection, "ORDER", str(order_id), "CANCELADO", {"status": order["status"]}, {"status": "CANCELADO"}, user_id)
        result = serialize_order(connection, order_id); _save_idempotent(connection, idempotency_key, "CANCELAR_PEDIDO", result); return result


def undo_order_action(order_id: int, user_id: int = 1, idempotency_key: str | None = None) -> dict:
    """Desfaz cancelamento ou conclusão, mantendo o histórico da correção."""
    with transaction(immediate=True) as connection:
        previous = _idempotent_result(connection, idempotency_key, "DESFAZER_PEDIDO")
        if previous:
            return previous
        order = connection.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            raise OrderNotFoundError("Pedido não encontrado.")
        now = utc_now()
        previous_status = order["status"]
        competence = (order["concluded_at"] or order["confirmed_at"] or order["source_created_at"] or order["created_at"] or "")[:7]
        if order["status"] == "CANCELADO":
            connection.execute("UPDATE orders SET status='PENDENTE',cancelled_at=NULL,updated_at=? WHERE id=?", (now, order_id))
            _audit(connection, "ORDER", str(order_id), "DESFAZIMENTO_CANCELAMENTO", {"status": "CANCELADO"}, {"status": "PENDENTE"}, user_id)
        elif order["status"] == "CONCLUIDO":
            connection.execute("UPDATE orders SET status='APROVADO',concluded_at=NULL,updated_at=? WHERE id=?", (now, order_id))
            _audit(connection, "ORDER", str(order_id), "DESFAZIMENTO_CONCLUSAO", {"status": "CONCLUIDO"}, {"status": "APROVADO"}, user_id)
        elif order["status"] == "APROVADO":
            movements_to_reverse = connection.execute("SELECT * FROM stock_movements WHERE order_id=? AND movement_type='BAIXA_PEDIDO' ORDER BY id", (order_id,)).fetchall()
            if not movements_to_reverse:
                raise InvalidOrderStatusError("Não há baixa registrada para desfazer neste pedido.")
            for movement in movements_to_reverse:
                inventory = connection.execute("SELECT * FROM inventory WHERE product_id=?", (movement["product_id"],)).fetchone()
                before = inventory["physical_quantity"]
                after = before + movement["quantity"]
                connection.execute("UPDATE inventory SET physical_quantity=?,version=version+1,updated_at=? WHERE product_id=?", (after, now, movement["product_id"]))
                connection.execute("INSERT INTO stock_movements(product_id,movement_type,quantity,quantity_before,quantity_after,reserved_before,reserved_after,reference_type,reference_id,order_id,reason,user_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (movement["product_id"], "DEVOLUCAO", movement["quantity"], before, after, inventory["reserved_quantity"], inventory["reserved_quantity"], "DESFAZIMENTO_APROVACAO", str(order_id), order_id, "Desfazimento da aprovação do pedido", user_id, now))
            connection.execute("UPDATE orders SET status='PENDENTE',confirmed_at=NULL,updated_at=? WHERE id=?", (now, order_id))
            _audit(connection, "ORDER", str(order_id), "DESFAZIMENTO_APROVACAO", {"status": "APROVADO"}, {"status": "PENDENTE"}, user_id)
        else:
            raise InvalidOrderStatusError("Só é possível desfazer um cancelamento, uma conclusão ou uma aprovação recente.")
        result = serialize_order(connection, order_id)
        _save_idempotent(connection, idempotency_key, "DESFAZER_PEDIDO", result)
    if settings.pedidos_concluidos_xlsx.exists() and previous_status in {"APROVADO", "CONCLUIDO"}:
        record_completed_order(settings.pedidos_concluidos_xlsx, result, result["items"], competence or datetime.now().strftime("%Y-%m"))
    return result


def conclude_order(order_id: int, user_id: int = 1, idempotency_key: str | None = None) -> dict:
    with transaction(immediate=True) as connection:
        previous = _idempotent_result(connection, idempotency_key, "CONCLUIR_PEDIDO")
        if previous: return previous
        order = connection.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order: raise OrderNotFoundError("Pedido não encontrado.")
        if order["status"] == "CONCLUIDO": return serialize_order(connection, order_id)
        if order["status"] != "APROVADO": raise InvalidOrderStatusError("Apenas pedidos aprovados podem ser concluídos.")
        now = utc_now(); connection.execute("UPDATE orders SET status='CONCLUIDO',concluded_at=?,updated_at=? WHERE id=?", (now, now, order_id))
        _audit(connection, "ORDER", str(order_id), "CONCLUIDO", {"status": order["status"]}, {"status": "CONCLUIDO"}, user_id)
        result = serialize_order(connection, order_id); _save_idempotent(connection, idempotency_key, "CONCLUIR_PEDIDO", result)
    if settings.pedidos_concluidos_xlsx.exists():
        record_completed_order(settings.pedidos_concluidos_xlsx, result, result["items"], datetime.now().strftime("%Y-%m"))
    return result


def add_stock(product_id: int, quantity: int, reason: str, supplier: str | None, user_id: int = 1) -> dict:
    if quantity <= 0 or not reason.strip(): raise ValueError("Quantidade e justificativa são obrigatórias.")
    with transaction(immediate=True) as connection:
        product = connection.execute("SELECT * FROM products WHERE id=? AND status='ATIVO'", (product_id,)).fetchone()
        if not product: raise ProductNotFoundError("Produto não encontrado.")
        inventory = connection.execute("SELECT * FROM inventory WHERE product_id=?", (product_id,)).fetchone(); now=utc_now()
        connection.execute("UPDATE inventory SET physical_quantity=physical_quantity+?,version=version+1,updated_at=? WHERE product_id=?", (quantity, now, product_id))
        connection.execute("INSERT INTO stock_movements(product_id,movement_type,quantity,quantity_before,quantity_after,reserved_before,reserved_after,reference_type,reason,user_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (product_id,"ENTRADA",quantity,inventory["physical_quantity"],inventory["physical_quantity"]+quantity,inventory["reserved_quantity"],inventory["reserved_quantity"],"REPOSICAO",reason,user_id,now))
        _audit(connection,"INVENTORY",str(product_id),"ENTRADA",dict(inventory),{"quantity":inventory["physical_quantity"]+quantity,"added_quantity":quantity,"product_name":product["name"],"supplier":supplier},user_id)
        return row_dict(connection.execute("SELECT p.*,i.physical_quantity,i.reserved_quantity,i.available_quantity FROM products p JOIN inventory i ON i.product_id=p.id WHERE p.id=?",(product_id,)).fetchone())


def withdraw_noncommercial(items: list[dict], reason_type: str, recipient: str, observation: str = "", user_id: int = 1) -> dict:
    labels = {
        "KIT_INTEGRACAO": "Kit de integração",
        "BRINDE": "Brinde",
        "USO_INTERNO": "Uso interno",
        "PERDA_AVARIA": "Perda ou avaria",
        "OUTRO": "Saída não comercial",
    }
    if reason_type not in labels:
        raise ValueError("Escolha um motivo válido para a saída.")
    if not recipient.strip():
        raise ValueError("Informe o colaborador ou destinatário.")
    if not isinstance(items, list) or not items:
        raise ValueError("Selecione pelo menos um produto.")

    quantities: dict[int, int] = {}
    for item in items:
        try:
            product_id = int(item.get("product_id", 0))
            quantity = int(item.get("quantity", 0))
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("Produto e quantidade são obrigatórios.") from error
        if product_id <= 0 or quantity <= 0:
            raise ValueError("Produto e quantidade devem ser maiores que zero.")
        quantities[product_id] = quantities.get(product_id, 0) + quantity

    operation_id = f"SAIDA-{uuid.uuid4().hex[:12].upper()}"
    reason_label = labels[reason_type]
    reason = f"{reason_label}: retirada para {recipient.strip()}"
    if observation.strip():
        reason = f"{reason}. {observation.strip()}"
    results = []
    summary_parts = []
    with transaction(immediate=True) as connection:
        for product_id, quantity in quantities.items():
            inventory = connection.execute(
                "SELECT i.*,p.name FROM inventory i JOIN products p ON p.id=i.product_id WHERE i.product_id=? AND p.status='ATIVO'",
                (product_id,),
            ).fetchone()
            if not inventory:
                raise ProductNotFoundError("Produto não encontrado.")
            available = inventory["physical_quantity"] - inventory["reserved_quantity"]
            if quantity > available:
                raise InsufficientStockError(f"Estoque disponível insuficiente para {inventory['name']}. Disponível: {available}.")
            after = inventory["physical_quantity"] - quantity
            now = utc_now()
            connection.execute(
                "UPDATE inventory SET physical_quantity=?,version=version+1,updated_at=? WHERE product_id=?",
                (after, now, product_id),
            )
            connection.execute(
                "INSERT INTO stock_movements(product_id,movement_type,quantity,quantity_before,quantity_after,reserved_before,reserved_after,reference_type,reference_id,reason,user_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (product_id, "SAIDA", quantity, inventory["physical_quantity"], after, inventory["reserved_quantity"], inventory["reserved_quantity"], "SAIDA_NAO_COMERCIAL", operation_id, reason, user_id, now),
            )
            _audit(
                connection,
                "INVENTORY",
                str(product_id),
                "SAIDA_NAO_COMERCIAL",
                dict(inventory),
                {"product_name": inventory["name"], "quantity": quantity, "quantity_after": after, "reason_label": reason_label, "recipient": recipient.strip(), "observation": observation.strip()},
                user_id,
                operation_id,
                write_log=False,
            )
            summary_parts.append(f"{quantity} {inventory['name']}")
            results.append({"product_id": product_id, "produto": inventory["name"], "quantidade": quantity, "saldo": after})

    summary = ", ".join(summary_parts)
    append_log("AUDITORIA", {"action": "SAIDA_NAO_COMERCIAL", "entity_id": operation_id, "items_summary": summary, "reason_label": reason_label, "recipient": recipient.strip()})
    return {"operacao": operation_id, "motivo": reason_label, "destinatario": recipient.strip(), "itens": results}


def preview_stock_import() -> dict:
    if not settings.estoque_xlsx.exists():
        raise ValueError(f"Planilha de estoque não encontrada: {settings.estoque_xlsx.name}")
    source_rows = read_current_stock_rows(settings.estoque_xlsx)
    with connect() as connection:
        products = connection.execute("SELECT p.id,p.name,p.base_name,p.size,i.physical_quantity,i.reserved_quantity FROM products p JOIN inventory i ON i.product_id=p.id WHERE p.status='ATIVO'").fetchall()
        changes = []
        unresolved = []
        not_registered = []
        for source in source_rows:
            candidates = [product for product in products if normalize(product["base_name"]) == normalize(source["base_name"]) and normalize(product["size"] or "Único") == normalize(source["size"])]
            if not candidates:
                not_registered.append({"produto": source["base_name"], "tamanho": source["size"], "quantidade": source["quantity"], "motivo": "Produto ainda não cadastrado"})
                continue
            if len(candidates) > 1:
                # Quando a planilha informa zero, o resultado é seguro para todas as
                # variações: nenhuma delas pode ter saldo físico naquele local.
                if source["quantity"] == 0:
                    for product in candidates:
                        changes.append({"product_id": product["id"], "produto": product["name"], "atual": product["physical_quantity"], "reservado": product["reserved_quantity"], "novo": 0, "locais": source["locations"], "diferenca": -product["physical_quantity"]})
                    continue
                unresolved.append({"produto": source["base_name"], "tamanho": source["size"], "quantidade": source["quantity"], "motivo": "Mais de uma variação de cor encontrada"})
                continue
            product = candidates[0]
            changes.append({"product_id": product["id"], "produto": product["name"], "atual": product["physical_quantity"], "reservado": product["reserved_quantity"], "novo": source["quantity"], "locais": source["locations"], "diferenca": source["quantity"] - product["physical_quantity"]})
        return {"arquivo": settings.estoque_xlsx.name, "locais": ["EUGENIO", "GRAND_STATION"], "alteracoes": changes, "nao_identificados": unresolved, "nao_cadastrados": not_registered, "total_alteracoes": sum(1 for change in changes if change["diferenca"] != 0)}


def import_current_stock(user_id: int = 1) -> dict:
    preview = preview_stock_import()
    if preview["nao_identificados"]:
        raise ValueError("Existem produtos não identificados na planilha. Revise a prévia antes de importar.")
    now = utc_now()
    updated = []
    with transaction(immediate=True) as connection:
        for change in preview["alteracoes"]:
            if change["novo"] < change["reservado"]:
                raise ValueError(f"O estoque de {change['produto']} não pode ficar abaixo das unidades reservadas.")
            if change["novo"] == change["atual"]:
                continue
            connection.execute("UPDATE inventory SET physical_quantity=?,version=version+1,updated_at=? WHERE product_id=?", (change["novo"], now, change["product_id"]))
            connection.execute("INSERT INTO stock_movements(product_id,movement_type,quantity,quantity_before,quantity_after,reserved_before,reserved_after,reference_type,reason,user_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (change["product_id"], "AJUSTE", abs(change["diferenca"]), change["atual"], change["novo"], change["reservado"], change["reservado"], "INVENTARIO", "Reconciliação do estoque físico pela planilha atual", user_id, now))
            _audit(connection, "INVENTORY", str(change["product_id"]), "RECONCILIACAO", {"physical_quantity": change["atual"]}, {"product_name": change["produto"], "old_quantity": change["atual"], "new_quantity": change["novo"], "locations": change["locais"]}, user_id)
            updated.append(change)
    return {"atualizados": len(updated), "sem_alteracao": preview["total_alteracoes"] - len(updated), "itens": updated}


def dashboard() -> dict:
    with connect() as connection:
        pending = connection.execute("SELECT COUNT(*) FROM orders WHERE status='PENDENTE'").fetchone()[0]
        approved_today = connection.execute("SELECT COUNT(*) FROM orders WHERE status='APROVADO' AND date(confirmed_at)=date('now')").fetchone()[0]
        concluded = connection.execute("SELECT COUNT(*) FROM orders WHERE status='CONCLUIDO'").fetchone()[0]
        low = connection.execute("SELECT COUNT(*) FROM inventory WHERE available_quantity <= (SELECT CAST(value AS INTEGER) FROM settings WHERE key='stock_low_limit')").fetchone()[0]
        total = connection.execute("SELECT COALESCE(SUM(available_quantity),0) FROM inventory").fetchone()[0]
        return {"pedidos_pendentes":pending,"pedidos_aprovados_hoje":approved_today,"pedidos_concluidos":concluded,"produtos_estoque_baixo":low,"total_itens_disponiveis":total}


def movements(page: int = 1, page_size: int = 50) -> dict:
    page_size=min(max(page_size,1),100); offset=(max(page,1)-1)*page_size
    with connect() as connection:
        total=connection.execute("SELECT COUNT(*) FROM stock_movements").fetchone()[0]
        rows=connection.execute("SELECT m.*,p.name AS product_name,o.order_number,u.name AS responsible FROM stock_movements m JOIN products p ON p.id=m.product_id LEFT JOIN orders o ON o.id=m.order_id LEFT JOIN users u ON u.id=m.user_id ORDER BY m.created_at DESC LIMIT ? OFFSET ?",(page_size,offset)).fetchall()
        return {"items":[row_dict(row) for row in rows],"page":page,"page_size":page_size,"total_items":total,"total_pages":(total+page_size-1)//page_size}


def completed_summary(competence: str | None = None) -> list[dict]:
    with connect() as connection:
        rows = connection.execute("SELECT id FROM orders WHERE status IN ('APROVADO','CONCLUIDO') AND (? IS NULL OR substr(COALESCE(concluded_at, confirmed_at, source_created_at, created_at),1,7)=?) ORDER BY COALESCE(concluded_at, confirmed_at, source_created_at, created_at) DESC, id DESC", (competence, competence)).fetchall()
        data = []
        for row in rows:
            order = serialize_order(connection, row["id"])
            order["products"] = ", ".join(item["product_name_snapshot"].rsplit(" - ", 1)[0] if " - " in item["product_name_snapshot"] else item["product_name_snapshot"] for item in order["items"])
            order["quantities"] = ", ".join(str(item["quantity"]) for item in order["items"])
            order["sizes"] = ", ".join(item["product_name_snapshot"].rsplit(" - ", 1)[1] if " - " in item["product_name_snapshot"] else "Único" for item in order["items"])
            order["competence"] = (order.get("concluded_at") or order.get("confirmed_at") or order.get("source_created_at") or order.get("created_at") or "")[:7]
            order["situation"] = "Concluído" if order["status"] == "CONCLUIDO" else "Aprovado"
            data.append(order)
    if settings.pedidos_concluidos_xlsx.exists():
        for order in data:
            record_completed_order(settings.pedidos_concluidos_xlsx, order, order["items"], order["competence"] or datetime.now().strftime("%Y-%m"))
    return data


def adjust_inventory(product_id: int, delta: int, adjustment_type: str, reason: str, user_id: int = 1) -> dict:
    if delta == 0 or not reason.strip():
        raise ValueError("O ajuste deve possuir quantidade diferente de zero e justificativa.")
    allowed = {"CORRECAO_POSITIVA", "CORRECAO_NEGATIVA", "ENTRADA_MERCADORIA", "AVARIA", "PERDA", "AJUSTE_INVENTARIO"}
    if adjustment_type not in allowed:
        raise ValueError("Tipo de ajuste inválido.")
    with transaction(immediate=True) as connection:
        inventory = connection.execute("SELECT i.*,p.name FROM inventory i JOIN products p ON p.id=i.product_id WHERE i.product_id=? AND p.status='ATIVO'", (product_id,)).fetchone()
        if not inventory:
            raise ProductNotFoundError("Produto não encontrado.")
        after = inventory["physical_quantity"] + delta
        if after < inventory["reserved_quantity"]:
            raise InsufficientStockError("O ajuste não pode deixar o estoque abaixo do que já está reservado.")
        now = utc_now()
        connection.execute("UPDATE inventory SET physical_quantity=?,version=version+1,updated_at=? WHERE product_id=?", (after, now, product_id))
        connection.execute("INSERT INTO stock_movements(product_id,movement_type,quantity,quantity_before,quantity_after,reserved_before,reserved_after,reference_type,reason,user_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (product_id, "AJUSTE", abs(delta), inventory["physical_quantity"], after, inventory["reserved_quantity"], inventory["reserved_quantity"], adjustment_type, reason, user_id, now))
        _audit(connection, "INVENTORY", str(product_id), "AJUSTE_MANUAL", dict(inventory), {"quantity_after": after, "product_name": inventory["name"], "type": adjustment_type, "reason": reason}, user_id)
        return row_dict(connection.execute("SELECT p.*,i.physical_quantity,i.reserved_quantity,i.available_quantity FROM products p JOIN inventory i ON i.product_id=p.id WHERE p.id=?", (product_id,)).fetchone())


def return_order(order_id: int, product_id: int, quantity: int, reason: str, user_id: int = 1) -> dict:
    if quantity <= 0 or not reason.strip():
        raise ValueError("Quantidade e motivo da devolução são obrigatórios.")
    with transaction(immediate=True) as connection:
        order = connection.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            raise OrderNotFoundError("Pedido não encontrado.")
        if order["status"] not in {"APROVADO", "CONCLUIDO"}:
            raise InvalidOrderStatusError("Apenas pedidos aprovados ou concluídos podem ter devolução.")
        item = connection.execute("SELECT * FROM order_items WHERE order_id=? AND product_id=?", (order_id, product_id)).fetchone()
        if not item:
            raise ProductNotFoundError("Produto não pertence ao pedido informado.")
        returned = connection.execute("SELECT COALESCE(SUM(quantity),0) FROM stock_movements WHERE order_id=? AND product_id=? AND movement_type='DEVOLUCAO'", (order_id, product_id)).fetchone()[0]
        if returned + quantity > item["quantity"]:
            raise ValueError("A devolução excede a quantidade do item no pedido.")
        inventory = connection.execute("SELECT * FROM inventory WHERE product_id=?", (product_id,)).fetchone(); now = utc_now()
        connection.execute("UPDATE inventory SET physical_quantity=physical_quantity+?,version=version+1,updated_at=? WHERE product_id=?", (quantity, now, product_id))
        connection.execute("INSERT INTO stock_movements(product_id,movement_type,quantity,quantity_before,quantity_after,reserved_before,reserved_after,reference_type,reference_id,order_id,reason,user_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (product_id,"DEVOLUCAO",quantity,inventory["physical_quantity"],inventory["physical_quantity"]+quantity,inventory["reserved_quantity"],inventory["reserved_quantity"],"PEDIDO",str(order_id),order_id,reason,user_id,now))
        _audit(connection,"ORDER",str(order_id),"DEVOLUCAO",None,{"product_id":product_id,"quantity":quantity,"reason":reason},user_id)
        return serialize_order(connection, order_id)


def request_replenishment(product_id: int, quantity: int, responsible: str, supplier: str | None = None, expected_at: str | None = None) -> dict:
    if quantity <= 0 or not responsible.strip():
        raise ValueError("Quantidade e responsável são obrigatórios.")
    with transaction(immediate=True) as connection:
        if not connection.execute("SELECT 1 FROM products WHERE id=? AND status='ATIVO'", (product_id,)).fetchone():
            raise ProductNotFoundError("Produto não encontrado.")
        now = utc_now()
        replenishment_id = connection.execute("INSERT INTO replenishments(product_id,supplier,requested_quantity,requested_at,responsible,expected_at,status) VALUES (?,?,?,?,?,?,?)", (product_id,supplier,quantity,now,responsible,expected_at,"SOLICITADO")).lastrowid
        _notification(connection,"REPOSICAO","Reposição registrada",f"Reposição de {quantity} unidade(s) solicitada.","replenishment",str(replenishment_id))
        return row_dict(connection.execute("SELECT r.*,p.name AS product_name FROM replenishments r JOIN products p ON p.id=r.product_id WHERE r.id=?", (replenishment_id,)).fetchone())


def close_month(competence: str) -> dict:
    if not re.fullmatch(r"\d{4}-\d{2}", competence):
        raise ValueError("Competência deve estar no formato AAAA-MM.")
    with transaction(immediate=True) as connection:
        existing = connection.execute("SELECT * FROM monthly_closures WHERE competence=?", (competence,)).fetchone()
        if existing and existing["status"] != "ABERTO":
            raise InvalidOrderStatusError("Esta competência já foi fechada.")
        stats = connection.execute("SELECT COUNT(DISTINCT customer_identifier) collaborators_count,COUNT(*) orders_count,COALESCE(SUM(total_cents),0) total_cents FROM orders WHERE status='CONCLUIDO' AND substr(concluded_at,1,7)=?", (competence,)).fetchone()
        now = utc_now()
        if existing:
            connection.execute("UPDATE monthly_closures SET collaborators_count=?,orders_count=?,total_cents=?,status='FECHADO',closed_at=?,updated_at=? WHERE id=?", (stats[0],stats[1],stats[2],now,now,existing["id"]))
            closure_id = existing["id"]
        else:
            closure_id = connection.execute("INSERT INTO monthly_closures(competence,collaborators_count,orders_count,total_cents,status,closed_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)", (competence,stats[0],stats[1],stats[2],"FECHADO",now,now,now)).lastrowid
        return row_dict(connection.execute("SELECT * FROM monthly_closures WHERE id=?", (closure_id,)).fetchone())


def export_closure_csv(competence: str) -> str:
    with connect() as connection:
        rows = connection.execute("SELECT customer_name,customer_email,? AS competence,COUNT(*) orders_count,ROUND(SUM(total_cents)/100.0,2) total FROM orders WHERE status='CONCLUIDO' AND substr(concluded_at,1,7)=? GROUP BY customer_identifier,customer_name,customer_email ORDER BY customer_name", (competence,competence)).fetchall()
        output = io.StringIO(); writer = csv.writer(output); writer.writerow(["Nome","E-mail","Competência","Quantidade de pedidos","Valor total para desconto"])
        for row in rows: writer.writerow([row["customer_name"],row["customer_email"] or "",row["competence"],row["orders_count"],row["total"]])
        return output.getvalue()
