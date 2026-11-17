from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .auth import google_login_url, login_from_callback, oauth_enabled, revoke_session, session_from_token
from .clerk_auth import clerk_configured, clerk_identity
from .db import connect, initialize, utc_now
from .config import settings
from .errors import DomainError
from .services import (
    add_stock,
    adjust_inventory,
    append_log,
    approve_order,
    cancel_order,
    completed_summary,
    conclude_order,
    dashboard,
    get_order,
    import_current_stock,
    list_orders,
    list_store_orders,
    list_store_products,
    list_products,
    movements,
    preview_stock_import,
    close_month,
    export_closure_csv,
    request_replenishment,
    return_order,
    withdraw_noncommercial,
    undo_order_action,
    hydrate_prices_from_history,
    seed_catalog_from_xlsx,
    sync_google_orders,
    create_store_order,
    list_product_images,
    product_image_file,
    remove_product_image,
    save_product_image,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("lojinha-klubi")


def json_response(handler: BaseHTTPRequestHandler, payload: object, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    origin = handler.headers.get("Origin", "")
    if origin in settings.allowed_origins:
        handler.send_header("Access-Control-Allow-Origin", origin)
        handler.send_header("Vary", "Origin")
        handler.send_header("Access-Control-Allow-Credentials", "true")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-API-Token, X-User-Role, X-Idempotency-Key, X-Request-Id")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("X-Request-Id", getattr(handler, "request_id", ""))
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def file_response(handler: BaseHTTPRequestHandler, path, content_type: str) -> None:
    body = path.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    # O nome do arquivo muda a cada envio, então a foto pode ficar em cache por bastante tempo.
    handler.send_header("Cache-Control", "public, max-age=31536000, immutable")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def redirect_response(handler: BaseHTTPRequestHandler, location: str, cookies: list[str] | None = None) -> None:
    handler.send_response(302)
    handler.send_header("Location", location)
    for cookie in cookies or []:
        handler.send_header("Set-Cookie", cookie)
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def csv_response(handler: BaseHTTPRequestHandler, content: str, filename: str) -> None:
    body = content.encode("utf-8-sig")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/csv; charset=utf-8")
    handler.send_header("Content-Disposition", f'attachment; filename="{filename}"')
    origin = handler.headers.get("Origin", "")
    if origin in settings.allowed_origins:
        handler.send_header("Access-Control-Allow-Origin", origin)
        handler.send_header("Vary", "Origin")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


MAX_BODY_BYTES = 6 * 1024 * 1024  # foto de até 3 MB em base64, com folga


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "LojinhaKlubi/1.0"

    def log_message(self, format, *args):
        logger.info("%s - %s", self.address_string(), format % args)

    def _request_context(self) -> None:
        self.request_id = self.headers.get("X-Request-Id") or str(uuid.uuid4())

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > MAX_BODY_BYTES:
            raise ValueError("O envio é grande demais. Fotos devem ter no máximo 3 MB.")
        return json.loads(self.rfile.read(length) or b"{}")

    def _session(self) -> dict | None:
        raw_cookie = self.headers.get("Cookie", "")
        cookies = {}
        for item in raw_cookie.split(";"):
            if "=" in item:
                key, value = item.strip().split("=", 1)
                cookies[key] = value
        return session_from_token(cookies.get("klubi_session"))

    def _clerk_identity(self) -> dict | None:
        if not hasattr(self, "_clerk_identity_value"):
            self._clerk_identity_value = clerk_identity({key: value for key, value in self.headers.items()})
        return self._clerk_identity_value

    def _identity(self) -> dict | None:
        return self._clerk_identity() or self._session()

    def _role(self) -> str:
        tokens = {token: role for token, role in ((settings.admin_token, "ADMINISTRADOR"), (settings.operation_token, "OPERACAO"), (settings.consultation_token, "CONSULTA")) if token}
        return tokens.get(self.headers.get("X-API-Token", ""), "") or (self._identity() or {}).get("role", "")

    def _authorized(self, roles: set[str]) -> bool:
        role = self._role()
        if not role:
            json_response(self, {"detail": "É necessário informar um token de acesso válido."}, 401)
            return False
        if role not in roles:
            json_response(self, {"detail": "Seu perfil não possui permissão para esta ação."}, 403)
            return False
        return True

    def do_OPTIONS(self):
        self._request_context()
        json_response(self, {}, 204)

    def do_GET(self):
        self._request_context()
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        path = parsed.path.rstrip("/")
        try:
            if path == "/api/auth/google/start":
                return redirect_response(self, google_login_url())
            if path == "/api/auth/google/callback":
                if query.get("error"):
                    return redirect_response(self, f"{settings.frontend_url}/loja?login=cancelado")
                token = login_from_callback(query.get("code", [""])[0], query.get("state", [""])[0])
                return redirect_response(self, f"{settings.frontend_url}/loja", [f"klubi_session={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age=28800"])
            if path == "/api/auth/me":
                identity = self._identity()
                return json_response(self, {"authenticated": bool(identity), "oauth_configured": clerk_configured(), "user": identity})
            if path == "/api/loja/produtos":
                if not self._authorized({"ADMINISTRADOR", "COLABORADOR"}): return
                return json_response(self, list_store_products())
            if path == "/api/loja/meus-pedidos":
                identity = self._identity()
                if not identity or identity["role"] not in {"ADMINISTRADOR", "COLABORADOR"}:
                    if not self._authorized({"ADMINISTRADOR"}): return
                    identity = {"email": ""}
                return json_response(self, {"items": list_store_orders(identity["email"])})
            match = re.fullmatch(r"/api/imagens/([^/]+)", path)
            if match:
                found = product_image_file(match.group(1))
                if not found: return json_response(self, {"detail": "Foto não encontrada."}, 404)
                return file_response(self, *found)
            if path == "/api/saude": return json_response(self, {"status": "ok", "aplicacao": "LOJINHA KLUBI", "hora": utc_now()})
            if not self._authorized({"ADMINISTRADOR", "OPERACAO", "CONSULTA"}): return
            if path == "/api/dashboard": return json_response(self, dashboard())
            if path == "/api/produtos": return json_response(self, list_products(_int(query, "page", 1), _int(query, "page_size", 50), _bool(query, "baixo")))
            if path == "/api/produtos/imagens": return json_response(self, list_product_images())
            if path == "/api/estoque/baixo": return json_response(self, list_products(_int(query, "page", 1), _int(query, "page_size", 50), True))
            if path == "/api/estoque/importar":
                if not self._authorized({"ADMINISTRADOR", "OPERACAO"}): return
                return json_response(self, preview_stock_import())
            if path == "/api/pedidos": return json_response(self, list_orders(_str(query, "status"), _str(query, "busca"), _int(query, "page", 1), _int(query, "page_size", 50)))
            match = re.fullmatch(r"/api/pedidos/(\d+)", path)
            if match: return json_response(self, get_order(int(match.group(1))))
            if path == "/api/movimentacoes": return json_response(self, movements(_int(query, "page", 1), _int(query, "page_size", 50)))
            if path == "/api/pedidos-concluidos": return json_response(self, {"items": completed_summary()})
            if path == "/api/notificacoes":
                with connect() as connection:
                    return json_response(self, {"items": [dict(row) for row in connection.execute("SELECT * FROM notifications ORDER BY created_at DESC LIMIT 50")]})
            if path == "/api/fechamento-mensal":
                with connect() as connection:
                    return json_response(self, {"items": [dict(row) for row in connection.execute("SELECT * FROM monthly_closures ORDER BY competence DESC")]})
            match = re.fullmatch(r"/api/fechamento-mensal/(\d{4}-\d{2})/csv", path)
            if match:
                return csv_response(self, export_closure_csv(match.group(1)), f"fechamento-{match.group(1)}.csv")
            if path == "/api/relatorios":
                with connect() as connection:
                    months = connection.execute("SELECT substr(created_at,1,7) competence,COUNT(*) orders_count,COALESCE(SUM(total_cents),0) total_cents FROM orders WHERE status IN ('APROVADO','CONCLUIDO') GROUP BY competence ORDER BY competence DESC").fetchall()
                    products = connection.execute("SELECT p.name,COALESCE(SUM(oi.quantity),0) quantity FROM order_items oi JOIN products p ON p.id=oi.product_id JOIN orders o ON o.id=oi.order_id WHERE o.status IN ('APROVADO','CONCLUIDO') GROUP BY p.id ORDER BY quantity DESC LIMIT 10").fetchall()
                    return json_response(self, {"por_mes": [{**dict(row), "total": round(row["total_cents"] / 100, 2)} for row in months], "produtos_mais_pedidos": [dict(row) for row in products]})
            if path == "/api/configuracoes":
                with connect() as connection:
                    return json_response(self, {row["key"]: row["value"] for row in connection.execute("SELECT key,value FROM settings")})
            return json_response(self, {"detail": "Rota não encontrada."}, 404)
        except DomainError as exc:
            return json_response(self, {"detail": str(exc)}, exc.status_code)
        except Exception:
            logger.exception("falha GET %s", path)
            return json_response(self, {"detail": "Falha inesperada no servidor."}, 500)

    def do_POST(self):
        self._request_context()
        path = urlparse(self.path).path.rstrip("/")
        try:
            if path == "/api/auth/logout":
                raw_cookie = self.headers.get("Cookie", "")
                session_token = next((item.strip().split("=", 1)[1] for item in raw_cookie.split(";") if item.strip().startswith("klubi_session=")), None)
                revoke_session(session_token)
                return json_response(self, {"ok": True}, 200)
            if path == "/api/loja/pedidos":
                identity = self._identity()
                if not identity or identity["role"] not in {"ADMINISTRADOR", "COLABORADOR"}:
                    if not self._authorized({"ADMINISTRADOR"}): return
                    identity = {"email": "admin@klubi.local", "name": "Administrador local"}
                payload = self._body()
                return json_response(self, create_store_order(payload.get("items", []), identity), 201)
            if path == "/api/sincronizar":
                if not self._authorized({"ADMINISTRADOR"}): return
                return json_response(self, sync_google_orders())
            if path == "/api/log":
                if not self._authorized({"ADMINISTRADOR", "OPERACAO", "CONSULTA"}): return
                payload = self._body()
                append_log("INTERACAO_USUARIO", {"action": str(payload.get("action", "ACAO_NAO_INFORMADA"))[:120], "details": payload.get("details", {})})
                return json_response(self, {"registrado": True}, 201)
            match = re.fullmatch(r"/api/fechamento-mensal/(\d{4}-\d{2})/fechar", path)
            if match:
                if not self._authorized({"ADMINISTRADOR"}): return
                return json_response(self, close_month(match.group(1)))
            match = re.fullmatch(r"/api/pedidos/(\d+)/(aprovar|cancelar|concluir)", path)
            if match:
                if not self._authorized({"ADMINISTRADOR", "OPERACAO"}): return
                order_id, action = int(match.group(1)), match.group(2)
                key = self.headers.get("X-Idempotency-Key")
                result = {"aprovar": approve_order, "cancelar": cancel_order, "concluir": conclude_order}[action](order_id, 1, key)
                return json_response(self, result)
            match = re.fullmatch(r"/api/pedidos/(\d+)/desfazer", path)
            if match:
                if not self._authorized({"ADMINISTRADOR", "OPERACAO"}): return
                return json_response(self, undo_order_action(int(match.group(1)), 1, self.headers.get("X-Idempotency-Key")))
            if path == "/api/estoque/entrada":
                if not self._authorized({"ADMINISTRADOR", "OPERACAO"}): return
                payload = self._body()
                result = add_stock(int(payload.get("product_id", 0)), int(payload.get("quantity", 0)), str(payload.get("reason", "")), payload.get("supplier"))
                return json_response(self, result, 201)
            if path == "/api/estoque/saida-nao-comercial":
                if not self._authorized({"ADMINISTRADOR", "OPERACAO"}): return
                payload = self._body()
                result = withdraw_noncommercial(payload.get("items", []), str(payload.get("reason_type", "")), str(payload.get("recipient", "")), str(payload.get("observation", "")))
                return json_response(self, result, 201)
            if path == "/api/estoque/importar":
                if not self._authorized({"ADMINISTRADOR", "OPERACAO"}): return
                return json_response(self, import_current_stock(), 201)
            if path == "/api/produtos/imagem":
                if not self._authorized({"ADMINISTRADOR", "OPERACAO"}): return
                payload = self._body()
                return json_response(self, save_product_image(str(payload.get("base_name", "")), str(payload.get("image", ""))), 201)
            if path == "/api/produtos/imagem/remover":
                if not self._authorized({"ADMINISTRADOR", "OPERACAO"}): return
                payload = self._body()
                return json_response(self, remove_product_image(str(payload.get("base_name", ""))))
            match = re.fullmatch(r"/api/estoque/(\d+)/ajustar", path)
            if match:
                if not self._authorized({"ADMINISTRADOR"}): return
                payload = self._body()
                result = adjust_inventory(int(match.group(1)), int(payload.get("delta", 0)), str(payload.get("adjustment_type", "")), str(payload.get("reason", "")))
                return json_response(self, result)
            match = re.fullmatch(r"/api/estoque/(\d+)/repor", path)
            if match:
                if not self._authorized({"ADMINISTRADOR", "OPERACAO"}): return
                payload = self._body()
                result = request_replenishment(int(match.group(1)), int(payload.get("quantity", 0)), str(payload.get("responsible", "")), payload.get("supplier"), payload.get("expected_at"))
                return json_response(self, result, 201)
            match = re.fullmatch(r"/api/pedidos/(\d+)/devolver", path)
            if match:
                if not self._authorized({"ADMINISTRADOR", "OPERACAO"}): return
                payload = self._body()
                result = return_order(int(match.group(1)), int(payload.get("product_id", 0)), int(payload.get("quantity", 0)), str(payload.get("reason", "")))
                return json_response(self, result)
            return json_response(self, {"detail": "Rota não encontrada."}, 404)
        except DomainError as exc:
            return json_response(self, {"detail": str(exc)}, exc.status_code)
        except ValueError as exc:
            return json_response(self, {"detail": str(exc)}, 400)
        except Exception:
            logger.exception("falha POST %s", path)
            return json_response(self, {"detail": "Falha inesperada no servidor."}, 500)


def _str(query: dict[str, list[str]], key: str) -> str | None:
    return query.get(key, [None])[0]


def _int(query: dict[str, list[str]], key: str, default: int) -> int:
    try: return max(1, int(query.get(key, [default])[0]))
    except (TypeError, ValueError): return default


def _bool(query: dict[str, list[str]], key: str) -> bool:
    return query.get(key, ["false"])[0].lower() in {"1", "true", "sim"}


def main() -> None:
    initialize()
    seeded = seed_catalog_from_xlsx()
    logger.info("preços históricos atualizados: %s produto(s)", hydrate_prices_from_history())
    logger.info("catálogo inicial: %s produto(s)", seeded)
    try:
        logger.info("sincronização inicial: %s", sync_google_orders())
    except Exception:
        logger.exception("sincronização inicial indisponível; a API continuará funcionando")

    def sync_loop() -> None:
        while True:
            time.sleep(60)
            try:
                result = sync_google_orders()
                if result.get("importados"):
                    logger.info("novos pedidos sincronizados: %s", result["importados"])
            except Exception:
                logger.exception("falha na sincronização periódica")

    threading.Thread(target=sync_loop, name="sincronizacao-google-sheets", daemon=True).start()
    server = ThreadingHTTPServer((settings.api_host, settings.api_port), ApiHandler)
    logger.info("API da LOJINHA KLUBI disponível em http://%s:%s", settings.api_host, server.server_address[1])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("encerrando servidor")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
