# SyncroWay: homologação e produção

## API no Render

Importe o repositório como Web Service ou use `render.yaml` (nenhum deploy é feito por este documento). Build: `pip install -r requirements.txt`. Start: `gunicorn run:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 60`. Migrações: `python -m flask --app run:app db upgrade` uma vez por release, antes de iniciar os workers. Se seu plano não oferecer pre-deploy, execute em job/console de release antes do start; não rode migrações simultaneamente em cada worker.

Configure APP_ENV=production, DATABASE_URL PostgreSQL com TLS, SECRET_KEY aleatória, CORS_ORIGINS com domínio exato do frontend e RATELIMIT_STORAGE_URI com Redis compartilhado. SQLite e limiter em memória são recusados em produção. PROXY_HOPS=1 confia em exatamente um proxy de entrada (Render) para IP/protocolo; use 0 se a API for acessada diretamente, para não confiar em cabeçalhos enviados pelo cliente. Não rode seed de demonstração no banco comercial. Crie contas administrativas com senha individual via processo operacional controlado.

O banco SQLite removido do versionamento não é apagado de computadores já existentes. A remoção não apaga o histórico Git; revise os dados antigos e troque credenciais de demonstração se elas foram reutilizadas.

## Migrações existentes

A revisão b1995c5a0eea era cópia da baseline 3bce94637dbf. Agora é uma revisão de compatibilidade sem DDL; ambas IDs foram mantidas. Um banco vazio instala normalmente. Em banco existente, faça backup, consulte `db current` e compare o schema. Se foi criado com `db.create_all()` e não tem revisão, **somente após verificar que todas as tabelas/colunas correspondem aos modelos**, faça `db stamp b1995c5a0eea` e `db check`. Não use stamp para esconder diferenças nem apague tabelas para corrigir migrações.

## Pagamentos

Inicialmente ASAAS_ENABLED=false: somente dinheiro/cartão na entrega. Para homologar online, configure ASAAS_ENABLED=true, ASAAS_ENV=sandbox, ASAAS_API_KEY e um ASAAS_WEBHOOK_TOKEN diferente da API key (32+ caracteres). Registre webhook HTTPS em `/api/webhooks/asaas`. Configure carteiras verificadas via `/api/admin/payments/store-wallets/<store_id>` antes de receber pedidos reais.

O frontend gera uma cobrança por pedido/loja e apresenta Pix ou fatura hospedada. Recebimento depende do webhook; pedido criado não significa pagamento recebido. Validar na sandbox: criação, confirmação, callback duplicado, callback atrasado, cobrança removida, vencida, reembolso e timeout. Após validar, use credenciais e carteiras de produção; não misture bancos sandbox e produção.

- Cobrança inconclusiva: não emitir outra. Admin POST `/api/admin/payments/orders/<id>/reconcile` com `{"external_id":"pay_..."}`; backend confere referência e valor remotamente.
- Cancelamento de cobrança pendente: admin POST `/api/admin/payments/orders/<id>/cancel`. Estoque só retorna após remoção confirmada.
- Pedido sem cobrança: cliente pode cancelar; `python -m flask --app run:app expire-orders` libera reservas online expiradas sem cobrança. Agende em cron de servidor a cada 5 minutos, com as mesmas variáveis e banco; não há agendamento externo criado automaticamente.
- PAYMENT_OVERDUE não libera estoque: uma cobrança vencida ainda exige conciliação.
- Reembolso parcial/chargeback/pagamento após cancelamento: revisão operacional no gateway. Não há estorno ou transferência bancária automática ao motorista.
- Pagamento na entrega: admin POST `/api/admin/orders/<id>/confirm-offline-payment`, corpo `{"reference":"comprovante..."}`. A confirmação gera auditoria. Liquidação interna exige pagamento confirmado e não comprova transferência bancária.

## Monitoramento e backup

`/api/health` verifica processo; `/api/ready` verifica conexão e tabela de pedidos. Configure monitor HTTPS para readiness e alertas de 5xx/cobranças inconclusivas nos logs. Não registrar tokens/documentos em logs de integração.

Provisione backups automáticos no provedor PostgreSQL e teste restauração em banco separado. Para exportação manual use `pg_dump --format=custom --file=syncroway-backup.dump` com conexão via variáveis PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE e PGSSLMODE=require, sem senha na linha de comando. Guarde o dump cifrado fora do servidor da API. Restore em banco de homologação: `pg_restore --no-owner --dbname=<banco_separado> syncroway-backup.dump`. Não restaure sobre produção sem plano de recuperação e backup recente.

A CI valida testes e migrações PostgreSQL. Antes da abertura comercial, execute a jornada dos cinco perfis e confirme estoque, pedidos, valores e entregas no ambiente publicado.
