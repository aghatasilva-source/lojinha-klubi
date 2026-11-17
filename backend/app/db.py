from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from .config import settings


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL CHECK (role IN ('ADMINISTRADOR', 'OPERACAO', 'CONSULTA')),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    base_name TEXT NOT NULL,
    description TEXT,
    category TEXT NOT NULL DEFAULT 'Lojinha',
    color TEXT,
    size TEXT,
    unit_price_cents INTEGER NOT NULL DEFAULT 0 CHECK (unit_price_cents >= 0),
    status TEXT NOT NULL DEFAULT 'ATIVO' CHECK (status IN ('ATIVO', 'INATIVO')),
    source_key TEXT UNIQUE,
    source_created_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inventory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL UNIQUE REFERENCES products(id) ON DELETE RESTRICT,
    physical_quantity INTEGER NOT NULL DEFAULT 0 CHECK (physical_quantity >= 0),
    reserved_quantity INTEGER NOT NULL DEFAULT 0 CHECK (reserved_quantity >= 0),
    available_quantity INTEGER GENERATED ALWAYS AS (physical_quantity - reserved_quantity) STORED,
    version INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    CHECK (reserved_quantity <= physical_quantity)
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_number TEXT NOT NULL UNIQUE,
    customer_identifier TEXT NOT NULL,
    customer_name TEXT NOT NULL DEFAULT 'Colaborador não informado',
    customer_email TEXT,
    status TEXT NOT NULL DEFAULT 'PENDENTE' CHECK (status IN ('PENDENTE', 'APROVADO', 'CANCELADO', 'CONCLUIDO')),
    subtotal_cents INTEGER NOT NULL DEFAULT 0 CHECK (subtotal_cents >= 0),
    total_cents INTEGER NOT NULL DEFAULT 0 CHECK (total_cents >= 0),
    source_key TEXT UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    confirmed_at TEXT,
    cancelled_at TEXT,
    concluded_at TEXT
);

CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE RESTRICT,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    sku_snapshot TEXT NOT NULL,
    product_name_snapshot TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents >= 0),
    total_price_cents INTEGER NOT NULL CHECK (total_price_cents >= 0),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stock_movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    movement_type TEXT NOT NULL CHECK (movement_type IN ('ENTRADA', 'SAIDA', 'RESERVA', 'LIBERACAO_RESERVA', 'BAIXA_PEDIDO', 'DEVOLUCAO', 'AJUSTE', 'REPOSICAO')),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    quantity_before INTEGER NOT NULL CHECK (quantity_before >= 0),
    quantity_after INTEGER NOT NULL CHECK (quantity_after >= 0),
    reserved_before INTEGER NOT NULL CHECK (reserved_before >= 0),
    reserved_after INTEGER NOT NULL CHECK (reserved_after >= 0),
    reference_type TEXT,
    reference_id TEXT,
    order_id INTEGER REFERENCES orders(id) ON DELETE RESTRICT,
    reason TEXT NOT NULL,
    user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT,
    request_id TEXT NOT NULL,
    ip_address TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    operation_type TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    read_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    session_token TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('ADMINISTRADOR', 'OPERACAO', 'CONSULTA', 'COLABORADOR')),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_auth_sessions_expiry ON auth_sessions(expires_at);

CREATE TABLE IF NOT EXISTS replenishments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE RESTRICT,
    supplier TEXT,
    requested_quantity INTEGER NOT NULL CHECK (requested_quantity > 0),
    requested_at TEXT NOT NULL,
    responsible TEXT NOT NULL,
    expected_at TEXT,
    status TEXT NOT NULL DEFAULT 'SOLICITADO' CHECK (status IN ('PENDENTE', 'SOLICITADO', 'RECEBIDO'))
);

CREATE TABLE IF NOT EXISTS monthly_closures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    competence TEXT NOT NULL UNIQUE,
    collaborators_count INTEGER NOT NULL DEFAULT 0,
    orders_count INTEGER NOT NULL DEFAULT 0,
    total_cents INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ABERTO' CHECK (status IN ('ABERTO', 'FECHADO', 'EXPORTADO')),
    closed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Uma foto por produto-base (ex.: "Camiseta Preta"), compartilhada por todos os tamanhos.
-- base_key é o base_name normalizado (sem acento, maiúsculo), para não depender de grafia.
CREATE TABLE IF NOT EXISTS product_images (
    base_key TEXT PRIMARY KEY,
    base_name TEXT NOT NULL,
    file_name TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_products_base_name ON products(base_name);
CREATE INDEX IF NOT EXISTS idx_inventory_product ON inventory(product_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at);
CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_identifier);
CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_movements_product ON stock_movements(product_id);
CREATE INDEX IF NOT EXISTS idx_movements_order ON stock_movements(order_id);
CREATE INDEX IF NOT EXISTS idx_movements_created_at ON stock_movements(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity_type, entity_id);
"""


def utc_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


class ManagedConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect() -> sqlite3.Connection:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(settings.database_path, timeout=30, isolation_level=None, factory=ManagedConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def initialize() -> None:
    with connect() as connection:
        connection.executescript(SCHEMA)
        order_columns = {row["name"] for row in connection.execute("PRAGMA table_info(orders)")}
        if "source_created_at" not in order_columns:
            connection.execute("ALTER TABLE orders ADD COLUMN source_created_at TEXT")
        existing = connection.execute("SELECT version FROM schema_migrations WHERE version = 1").fetchone()
        if not existing:
            connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)", (1, utc_now()))
        if not connection.execute("SELECT version FROM schema_migrations WHERE version = 2").fetchone():
            connection.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)", (2, utc_now()))
        now = utc_now()
        connection.execute(
            "INSERT OR IGNORE INTO users(name,email,role,created_at,updated_at) VALUES (?,?,?,?,?)",
            ("Administrador local", "admin@klubi.local", "ADMINISTRADOR", now, now),
        )
        connection.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('stock_low_limit',?)", (str(settings.stock_low_limit),))
        connection.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('closing_day',?)", (str(settings.closing_day),))


@contextmanager
def transaction(immediate: bool = False):
    connection = connect()
    try:
        connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
