from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _load_local_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_local_env()


def _path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


@dataclass(frozen=True)
class Settings:
    root: Path = ROOT
    database_path: Path = _path(os.getenv("DATABASE_PATH", "backend/data/lojinha.db"))
    google_credentials_file: Path = _path(os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "sheets.json"))
    spreadsheet_id: str = os.getenv("GOOGLE_SPREADSHEET_ID", "")
    sheet_gid: int = int(os.getenv("GOOGLE_SHEET_GID", "0"))
    sheet_name: str = os.getenv("GOOGLE_SHEET_NAME", "Pedidos")
    estoque_xlsx: Path = _path(os.getenv("ESTOQUE_XLSX", "CONTROLE DE ESTOQUE .xlsx"))
    pedidos_concluidos_xlsx: Path = _path(os.getenv("PEDIDOS_CONCLUIDOS_XLSX", "PEDIDOS CONCLUIDOS - 2026.xlsx"))
    stock_low_limit: int = int(os.getenv("STOCK_LOW_LIMIT", "20"))
    closing_day: int = int(os.getenv("CLOSING_DAY", "30"))
    timezone: str = os.getenv("TIMEZONE", "America/Sao_Paulo")
    admin_token: str = os.getenv("ADMIN_TOKEN", "")
    operation_token: str = os.getenv("OPERACAO_TOKEN", "")
    consultation_token: str = os.getenv("CONSULTA_TOKEN", "")
    frontend_url: str = os.getenv("FRONTEND_URL", "http://localhost:3000")
    google_oauth_client_id: str = os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
    google_oauth_client_secret: str = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET", "")
    google_oauth_redirect_uri: str = os.getenv("GOOGLE_OAUTH_REDIRECT_URI", "http://localhost:8000/api/auth/google/callback")
    google_workspace_domain: str = os.getenv("GOOGLE_WORKSPACE_DOMAIN", "example.com").lower()
    admin_emails: tuple[str, ...] = tuple(email.strip().lower() for email in os.getenv("ADMIN_EMAILS", "").split(",") if email.strip())
    clerk_secret_key: str = os.getenv("CLERK_SECRET_KEY", "")
    clerk_authorized_parties: tuple[str, ...] = tuple(origin.strip().rstrip("/") for origin in os.getenv("CLERK_AUTHORIZED_PARTIES", "http://localhost:3000,http://127.0.0.1:3000").split(",") if origin.strip())
    clerk_workspace_domain: str = os.getenv("CLERK_WORKSPACE_DOMAIN", "example.com").lower()
    # Endereço em que a API escuta. No servidor, use 0.0.0.0 (ou deixe 127.0.0.1 atrás de um proxy reverso).
    api_host: str = os.getenv("API_HOST", "127.0.0.1")
    api_port: int = int(os.getenv("API_PORT", "8000"))
    # Fotos dos produtos enviadas pelo painel. Fica fora do repositório, como o banco.
    images_dir: Path = _path(os.getenv("IMAGES_DIR", "backend/data/imagens"))
    # Sites que podem chamar a API pelo navegador (CORS): a interface e as origens autorizadas do Clerk.
    allowed_origins: tuple[str, ...] = tuple(dict.fromkeys(
        origin.strip().rstrip("/")
        for origin in [os.getenv("FRONTEND_URL", "http://localhost:3000"), *os.getenv("CLERK_AUTHORIZED_PARTIES", "http://localhost:3000,http://127.0.0.1:3000").split(",")]
        if origin.strip()
    ))


settings = Settings()
