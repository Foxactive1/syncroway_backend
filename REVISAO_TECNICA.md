# Revisão técnica do backend SyncroWay

## Diagnóstico inicial

1. O conjunto anexado não possuía `app/__init__.py` com `create_app()`. O único
   `__init__.py` recebido era o módulo de modelos, portanto `run.py`, `seed.py` e
   os testes não conseguiam formar sozinhos uma aplicação executável.
2. O checkout juntava produtos de várias lojas em um único pedido e uma única
   entrega, contrariando a regra central de operação do marketplace.
3. Carrinho, checkout e agendamento exigiam autenticação, mas não restringiam o
   perfil; lojista, motorista ou administrador poderiam executar ações de cliente.
4. Produtos de lojas que haviam pausado pedidos ainda podiam ser adicionados ao
   carrinho.
5. O catálogo agregado exibia serviços vinculados a lojas desativadas.
6. Valores decimais não finitos (`NaN` e infinito) poderiam atravessar a
   validação e provocar falhas ou dados financeiros inválidos.
7. Os modelos de transação existiam, porém o checkout não criava o ledger e as
   rotas administrativas de faturamento estavam ausentes.
8. O script de upgrade adicionava somente `products.minimum_stock`; bancos de
   sprints anteriores continuariam sem diversas colunas usadas pelos modelos.
9. Havia conversões diretas de IDs que poderiam gerar erro 500 para entradas
   malformadas e flags booleanas tratadas incorretamente a partir de strings.
10. A conclusão repetida de um agendamento poderia incrementar mais de uma vez
    a métrica do prestador.

## Correções implementadas

- application factory completa, CORS por origem, migrações e chaves estrangeiras
  habilitadas no SQLite;
- checkout atômico agrupado por loja, com um pedido e uma entrega para cada grupo;
- taxas de entrega específicas por loja e retorno retrocompatível para checkout
  de uma única loja;
- criação de movimentos de estoque e três lançamentos financeiros por pedido:
  comissão, recebível do lojista e taxa do motorista;
- endpoints de faturamento, listagem e liquidação de transações;
- RBAC de cliente aplicado ao carrinho, checkout e agenda;
- validação de loja apta a receber pedidos e filtragem de catálogo;
- validação numérica finita, coordenadas geográficas e IDs;
- transições de estado idempotentes para agendamentos;
- upgrade de esquema aditivo/idempotente com backfill dos principais campos
  legados de serviços, pedidos, itens e agendamentos;
- README de instalação, execução, migração e contrato do checkout;
- suíte ampliada de 1 para 4 testes integrados.

## Validação executada

- compilação de todos os módulos Python;
- seed em banco SQLite temporário: 5 usuários, 9 lojas, 15 produtos, 7 serviços
  e 15 movimentos iniciais;
- upgrade executado duas vezes consecutivas sem alteração destrutiva;
- 4 testes integrados aprovados;
- contrato conferido para as rotas administrativas financeiras.

## Recomendações para a próxima sprint

1. Versionar migrações oficiais no Alembic em vez de manter upgrades cumulativos.
2. Integrar gateway de pagamento e webhooks idempotentes para atualizar
   `payment_status`.
3. Calcular taxa de entrega no servidor por distância/loja; o cliente não deve
   ser a fonte de verdade do valor em produção.
4. Adicionar paginação padronizada às listagens administrativas e do catálogo.
5. Criar testes de concorrência reais no PostgreSQL para disputa de estoque e
   aceite simultâneo de entrega.
6. Registrar ações administrativas no modelo `AuditLog`.
