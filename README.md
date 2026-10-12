<a id="topo"></a>
<div align="center">

# 🚀 SyncroWay

**Marketplace regional de produtos e serviços: lojas, prestadores autônomos, motoristas e clientes em um só lugar.**

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=for-the-badge&logo=python&logoColor=white)](#-stack-de-desenvolvimento)
[![Flask](https://img.shields.io/badge/Flask-3-000000?style=for-the-badge&logo=flask&logoColor=white)](#-stack-de-desenvolvimento)
[![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-2-D71F00?style=for-the-badge&logo=sqlite&logoColor=white)](#-banco-de-dados)
[![Termux](https://img.shields.io/badge/Termux-Ready-4E2A84?style=for-the-badge&logo=android&logoColor=white)](#-como-executar-no-termux-android)

*Backend Flask + SQLAlchemy, frontend estático servido por HTTP local.*

</div>

---

<details open>
<summary><b>📑 Índice Interativo (Clique para navegar)</b></summary>

- [🛠️ Stack de Desenvolvimento](#-stack-de-desenvolvimento)
- [🏗️ Arquitetura e Fluxos](#-arquitetura-e-fluxos)
- [🚀 Como Executar](#-como-executar)
- [⚙️ Configuração e Banco de Dados](#-configuração-e-banco-de-dados)
- [🔑 Acessos de Demonstração](#-acessos-de-demonstração)
- [🔌 API — Endpoints](#-api--endpoints)
- [🚑 Troubleshooting e Testes](#-troubleshooting-e-testes)

</details>

---

## 🛠️ Stack de Desenvolvimento

<details>
<summary><b>Visualizar Tecnologias (Backend, Frontend e Infra)</b></summary>

**Backend**
- Python 3.11+ | Flask 3
- SQLAlchemy 2 + Flask-SQLAlchemy | Flask-Migrate (Alembic)
- PyJWT (access + refresh tokens) | Werkzeug security (hash de senha)
- python-dotenv para configuração

**Frontend**
- HTML5 + Bootstrap 5.3 (CDN) | Bootstrap Icons
- JavaScript ESM puro (sem bundler)
- `API_BASE` configurável em runtime via `localStorage`

**Infraestrutura**
- Termux (Android) — ambiente primário
- Windows PowerShell — ambiente secundário

</details>

[⬆ Voltar ao topo](#topo)

---

## 🏗️ Arquitetura e Fluxos

<details>
<summary><b>Visualizar Diagrama e Controle de Acesso</b></summary>

```text
Cliente (browser)
    │
    │ HTTP/JSON   Authorization: Bearer <access_token>
    ▼
Flask API  ──►  SQLAlchemy ORM  ──►  SQLite (dev)  /  PostgreSQL (prod)
    │
    │ static/
    ▼
Frontend HTML/JS/CSS (servido via http.server)

Mapeamento de Papéis (Roles):
 * customer: Catálogo, carrinho, checkout (/catalog, /cart, /checkout) e Agendamento (/appointments).
 * merchant: Dashboard, produtos, estoque (/merchant/*).
 * provider: Perfil, serviços, agenda (/provider/*).
 * driver: Entregas - aceitar, coletar, entregar (/driver/*).
 * admin: Financeiro, usuários, lojas (/admin/*).
Nota: require_auth exige usuário autenticado. require_roles(...) valida o papel específico.
</details>
⬆ Voltar ao topo
🚀 Como Executar
Escolha seu ambiente de desenvolvimento abaixo para expandir as instruções:
<details>
<summary><b>📱 Executar no Termux (Android)</b></summary>
Pré-requisito: Termux com Python 3.11+ e git.
# 1. Instalar (Cria .venv, instala dependências e cria instance/)
chmod +x termux-syncroway.sh
./termux-syncroway.sh install

# 2. Popular banco (Recria schema + dados de demonstração)
./termux-syncroway.sh seed

# 3. Subir backend (Porta 5000)
./termux-syncroway.sh backend

Em outra sessão do Termux:
# 4. Servir frontend (Porta 5500)
./termux-syncroway.sh frontend

</details>
<details>
<summary><b>💻 Executar no Windows (PowerShell)</b></summary>
Backend:
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

$env:RESET_DB_ON_SEED = "true"
python seed.py
python run.py

Frontend (em outro terminal):
cd frontend
python -m http.server 5500

</details>
⬆ Voltar ao topo
⚙️ Configuração e Banco de Dados
<details>
<summary><b>Detalhes do <code>.env</code> e Conexões</b></summary>
Crie backend/.env (nunca comite este arquivo):
APP_ENV=development
DATABASE_URL= # Vazio para usar SQLite
SECRET_KEY=   # Gere com: python -c "import secrets; print(secrets.token_hex(32))"
JWT_ACCESS_MINUTES=15
JWT_REFRESH_DAYS=7
CORS_ORIGINS=[http://127.0.0.1:5500](http://127.0.0.1:5500),http://localhost:5500
DEFAULT_TIMEZONE=America/Sao_Paulo
FLASK_DEBUG=true

 * SQLite (Padrão): Arquivo gerado em backend/instance/syncroway.db. PRAGMA foreign_keys=ON habilitado.
 * PostgreSQL / Supabase: Suportado via psycopg 3. No Termux, use a porta do pooler de transação (6543) em vez da conexão direta, devido a restrições de IPv6 no Android.
   </details>
⬆ Voltar ao topo
🔑 Acessos de Demonstração
> Senha global: Mudar@123
> 
| Perfil | E-mail de Acesso | Visão do Painel |
|---|---|---|
| 🛡️ Administrador | admin@syncroway.local | Financeiro, transações, moderação global |
| 🏪 Lojista | loja@syncroway.local | Produtos, estoque, movimentações |
| 🛠️ Prestador | prestador@syncroway.local | Serviços, agendamentos |
| 🛵 Motorista | motorista@syncroway.local | Entregas, ganhos, histórico |
| 👤 Cliente | cliente@syncroway.local | Pedidos, agendamentos |
⬆ Voltar ao topo
🔌 API — Endpoints
Prefixo: /api. Endpoints restritos exigem Authorization: Bearer <access_token>.
<details>
<summary>🔐 <b>Auth & Público</b></summary>
 * POST /auth/register - Cria conta cliente
 * POST /auth/login - Retorna tokens
 * POST /auth/refresh - Renova tokens
 * GET /catalog - Catálogo (produtos + serviços)
 * GET /stores/<id> - Detalhes da loja
   </details>
<details>
<summary>🛒 <b>Cliente (Customer)</b></summary>
 * GET | POST | PATCH | DELETE /cart/items - Gestão do carrinho
 * POST /checkout - Cria pedidos
 * POST /appointments - Agenda serviço
 * GET /orders - Lista pedidos
   </details>
<details>
<summary>🏪 <b>Lojista (Merchant)</b></summary>
 * GET /merchant/dashboard - KPIs e financeiro
 * POST | GET | PATCH | DELETE /merchant/products - Gestão de produtos
 * PATCH /merchant/products/<id>/stock - Ajuste de estoque
 * GET /merchant/appointments - Agendamentos recebidos
   </details>
<details>
<summary>🛵 <b>Motorista (Driver)</b></summary>
 * GET /driver/dashboard - Entregas e ganhos
 * PATCH /driver/status - Disponibilidade e localização
 * POST /driver/deliveries/<id>/accept - Aceitar entrega
 * PATCH /driver/deliveries/<id> - Atualizar para coletado/entregue
   </details>
⬆ Voltar ao topo
🚑 Troubleshooting e Testes
<details>
<summary><b>Verificar Soluções Comuns e Comandos de Teste</b></summary>
Solução de Problemas
 * SECRET_KEY deve possuir ao menos 32 caracteres:
   Execute python -c "import secrets; print(secrets.token_hex(32))" e atualize seu .env.
 * failed to resolve host 'db.seu-projeto.supabase.co':
   Altere a URL no .env para a porta do pooler (6543) ou deixe em branco para SQLite.
 * Frontend não conecta (CORS / Failed to fetch):
   Verifique se a API roda em http://127.0.0.1:5000/api/health. Se acessar via rede local, adicione o IP real em CORS_ORIGINS.
Comandos de Teste
Backend (Pytest):
cd backend
source .venv/bin/activate
pytest -q

Frontend (Contrato):
node tests/frontend-contract.mjs

Reinício Limpo do Banco (Dev):
cd backend
rm -f instance/syncroway.db
RESET_DB_ON_SEED=true python seed.py

</details>
⬆ Voltar ao topo

