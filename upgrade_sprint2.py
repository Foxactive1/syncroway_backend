"""Upgrade aditivo e idempotente para o esquema atual do SyncroWay.

O script preserva os dados. Colunas novas são adicionadas como anuláveis para
manter compatibilidade com SQLite e PostgreSQL; em seguida, os campos legados
essenciais são preenchidos. Para produção, a evolução seguinte deve ser feita
com revisões versionadas do Flask-Migrate/Alembic.
"""
from sqlalchemy import inspect, text

from app import create_app
from app.extensions import db


def _quoted(name):
    return db.engine.dialect.identifier_preparer.quote(name)


def _add_missing_columns():
    inspector = inspect(db.engine)
    existing_tables = set(inspector.get_table_names())
    added = []

    # Cria primeiro somente tabelas inteiramente novas.
    for table in db.metadata.sorted_tables:
        if table.name not in existing_tables:
            table.create(db.engine, checkfirst=True)
            existing_tables.add(table.name)

    inspector = inspect(db.engine)
    with db.engine.begin() as connection:
        for table in db.metadata.sorted_tables:
            existing = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing:
                    continue
                column_type = column.type.compile(dialect=db.engine.dialect)
                connection.execute(text(
                    f"ALTER TABLE {_quoted(table.name)} "
                    f"ADD COLUMN {_quoted(column.name)} {column_type}"
                ))
                added.append(f"{table.name}.{column.name}")
    return added


def _backfill_legacy_data():
    inspector = inspect(db.engine)
    tables = set(inspector.get_table_names())

    def columns(table):
        return {column["name"] for column in inspector.get_columns(table)}

    with db.engine.begin() as connection:
        if "services" in tables:
            service_columns = columns("services")
            if {"title", "name"}.issubset(service_columns):
                connection.execute(text(
                    "UPDATE services SET title = name WHERE title IS NULL"
                ))
            if {"provider_id", "store_id"}.issubset(service_columns) and "stores" in tables:
                connection.execute(text(
                    "UPDATE services SET provider_id = "
                    "(SELECT owner_id FROM stores WHERE stores.id = services.store_id) "
                    "WHERE provider_id IS NULL AND store_id IS NOT NULL"
                ))
            defaults = {
                "pricing_model": "fixed", "visit_fee": 0,
                "modality": "at_shop", "rating_avg": 0,
                "rating_count": 0, "active": True,
            }
            for field, value in defaults.items():
                if field in service_columns:
                    connection.execute(
                        text(f"UPDATE services SET {_quoted(field)} = :value WHERE {_quoted(field)} IS NULL"),
                        {"value": value},
                    )

        if "products" in tables and "minimum_stock" in columns("products"):
            connection.execute(text(
                "UPDATE products SET minimum_stock = 5 WHERE minimum_stock IS NULL"
            ))

        if "orders" in tables:
            order_columns = columns("orders")
            defaults = {
                "type": "product", "subtotal": 0, "delivery_fee": 0,
                "visit_fee": 0, "total_amount": 0, "commission_value": 0,
            }
            for field, value in defaults.items():
                if field in order_columns:
                    connection.execute(
                        text(f"UPDATE orders SET {_quoted(field)} = :value WHERE {_quoted(field)} IS NULL"),
                        {"value": value},
                    )

        if "order_items" in tables:
            item_columns = columns("order_items")
            if "item_type" in item_columns:
                connection.execute(text(
                    "UPDATE order_items SET item_type = 'product' WHERE item_type IS NULL"
                ))
            if {"total_price", "unit_price", "quantity"}.issubset(item_columns):
                connection.execute(text(
                    "UPDATE order_items SET total_price = unit_price * quantity "
                    "WHERE total_price IS NULL"
                ))
            if {"item_name", "product_id"}.issubset(item_columns) and "products" in tables:
                connection.execute(text(
                    "UPDATE order_items SET item_name = "
                    "(SELECT name FROM products WHERE products.id = order_items.product_id) "
                    "WHERE item_name IS NULL AND product_id IS NOT NULL"
                ))

        if "appointments" in tables:
            appointment_columns = columns("appointments")
            if {"provider_id", "service_id"}.issubset(appointment_columns) and "services" in tables:
                connection.execute(text(
                    "UPDATE appointments SET provider_id = "
                    "(SELECT provider_id FROM services WHERE services.id = appointments.service_id) "
                    "WHERE provider_id IS NULL"
                ))


def _create_missing_indexes():
    for table in db.metadata.sorted_tables:
        for index in table.indexes:
            try:
                index.create(db.engine, checkfirst=True)
            except Exception as exc:
                print(f"[AVISO] índice {index.name} não criado: {exc}")


def upgrade_database():
    added = _add_missing_columns()
    _backfill_legacy_data()
    _create_missing_indexes()
    print(f"[OK] {len(added)} coluna(s) adicionada(s)")
    for name in added:
        print(f"  + {name}")
    print("[OK] Upgrade concluído sem exclusão de dados")


if __name__ == "__main__":
    app = create_app()
    with app.app_context():
        upgrade_database()
