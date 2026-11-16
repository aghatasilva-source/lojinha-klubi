# Lojinha Klubi

Aplicação web para gerenciamento de pedidos, estoque, movimentações e fechamento mensal.

## Requisitos

- Python 3.10 ou superior
- Node.js e npm
- Credenciais de autenticação e, para integração com planilhas, credenciais do Google configuradas localmente

## Configuração

1. Copie `.env.example` para `.env` e preencha as configurações necessárias.
2. Copie `frontend/.env.example` para `frontend/.env.local` e configure a chave publicável do Clerk, se usar esse provedor.
3. Mantenha `sheets.json`, planilhas operacionais, bancos de dados e segredos fora do repositório.
4. Configure o domínio autorizado e os provedores de autenticação antes de disponibilizar a aplicação.

Os arquivos de exemplo usam valores fictícios. Não reutilize credenciais de exemplo nem coloque segredos em variáveis `NEXT_PUBLIC_*`; elas são incluídas no código enviado ao navegador. A autenticação por token compartilhado deve ser usada apenas em ambiente local isolado.

## Instalação e execução

No Windows, instale as dependências em terminais separados:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend/requirements.txt
npm --prefix frontend install
```

Inicie a API:

```powershell
python -m backend.app.main
```

Em outro terminal, inicie a interface:

```powershell
npm --prefix frontend run dev
```

Acesse http://localhost:3000. O script `iniciar_lojinhas.bat` também pode ser usado quando o Node.js portátil estiver disponível em `.tools\node`.

## Testes

```powershell
python -m unittest discover -s backend/tests -v
```
