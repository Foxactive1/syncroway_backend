# API administrativa para cliente Delphi

Rotas adicionais, restritas a admin. O frontend web não foi alterado.

- GET/POST `/api/admin/management/users`, PATCH `/api/admin/management/users/<id>`.
- GET/POST `/api/admin/management/stores`, PATCH `/api/admin/management/stores/<id>`.
- GET/POST `/api/admin/management/drivers`, PATCH `/api/admin/management/drivers/<id>`.

Não há DELETE: PATCH com `active: false` arquiva/desativa sem apagar histórico. Lista limitada a 500 registros. A seleção de proprietário/categoria usa IDs da API. `commission_percent` recebe porcentagem humana (12.5), convertida para commission_rate=0.125 no backend, aplicável a novos pedidos. O frontend web segue usando as rotas antigas, sem alteração de contrato.

O backend bloqueia auto-desativação/rebaixamento do admin, garante admin ativo, exige transferência das lojas antes de retirar o papel de proprietário, impede a alteração do papel de motorista e alterações incompatíveis com entrega em andamento. Senha vazia/ausente em PATCH mantém a atual. Auditoria registra alterações, sem senhas/hashes/tokens. Não concede motorista completo via users: use drivers.

Implantação: aplicar PR e redeploy backend, com schema completo das migrações. Se audit_log faltar no banco legado, criar conforme modelo/migração antes de usar CRUD. Não execute seed ou apague tabelas. Auditoria e validação são no servidor, não dependem da UI Delphi.

Validação: testes de permissões, criação/edição/desativação, comissão, rollback, auditoria e proteção administrativa. Não houve alteração de dados na API de produção para testar os novos CRUDs.
