"""Add reusable media asset usage links.

Revision ID: 4b7e9d2a61c3
Revises: 7a4e2c91d6bf
"""
from alembic import op
import sqlalchemy as sa

revision = "4b7e9d2a61c3"
down_revision = "7a4e2c91d6bf"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "media_asset_usages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("asset_id", sa.Integer(), sa.ForeignKey("media_assets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("entity_type", sa.String(length=30), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=30), nullable=False, server_default="primary"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("entity_type", "entity_id", "role", name="uq_media_usage_entity_role"),
    )
    op.create_index("ix_media_asset_usages_asset_id", "media_asset_usages", ["asset_id"])
    op.create_index("ix_media_asset_usages_entity_type", "media_asset_usages", ["entity_type"])
    op.create_index("ix_media_asset_usages_entity_id", "media_asset_usages", ["entity_id"])


def downgrade():
    op.drop_table("media_asset_usages")
