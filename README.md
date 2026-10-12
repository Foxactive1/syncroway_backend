# SyncroWay API

Marketplace regional: clientes, lojistas, prestadores, motoristas e administradores.

Flask, SQLAlchemy, PostgreSQL em produção, SQLite em desenvolvimento, JWT com refresh rotativo e integração Asaas opcional. Frontend independente: https://github.com/Foxactive1/syncroway_frontend.

## Desenvolvimento

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Configure SECRET_KEY antes de iniciar.
python -m flask --app run:app db upgrade
python run.py
```

Dados de demonstração são opcionais: `python seed.py` apenas em ambiente de desenvolvimento. Não use seed de demonstração em produção. API `/api`; processo `/api/health`; prontidão `/api/ready`; configuração pública `/api/public-config`.

## Verificação

```bash
python -m pytest -q
python -m flask --app run:app db check
```

A CI também testa as migrações em PostgreSQL. Veja [DEPLOY.md](DEPLOY.md) para Render, variáveis, migração de bancos existentes, fluxo de pagamento, cancelamento, conciliação, backups e critérios de homologação. Publicar o código não configura contas Asaas, Redis, banco, domínio ou backups automaticamente.

## Comportamento financeiro

Frete definido pelo servidor para todas as formas de pagamento. Asaas desabilitado oferece apenas pagamento na entrega. Pedidos online reservam estoque; reservas sem cobrança expiram pelo comando `expire-orders`. Cobranças emitidas exigem conciliação antes de liberar estoque. Webhooks confirmam o pagamento; liquidação interna não comprova repasse bancário.
