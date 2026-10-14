"""Create reusable media asset library.

Revision ID: 7a4e2c91d6bf
Revises: b1995c5a0eea
"""
from alembic import op
import sqlalchemy as sa

revision = "7a4e2c91d6bf"
down_revision = "b1995c5a0eea"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "media_assets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("slug", sa.String(length=180), nullable=False),
        sa.Column("image_url", sa.Text(), nullable=False),
        sa.Column("category", sa.String(length=80), nullable=False, server_default="geral"),
        sa.Column("tags", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("alt_text", sa.String(length=300), nullable=True),
        sa.Column("source", sa.String(length=80), nullable=False, server_default="external"),
        sa.Column("license", sa.String(length=160), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("slug", name="uq_media_assets_slug"),
    )
    op.create_index("ix_media_assets_name", "media_assets", ["name"])
    op.create_index("ix_media_assets_category", "media_assets", ["category"])
    op.create_index("ix_media_assets_is_default", "media_assets", ["is_default"])
    op.create_index("ix_media_assets_active", "media_assets", ["active"])
    op.create_index("ix_media_assets_created_by", "media_assets", ["created_by"])


def downgrade():
    op.drop_table("media_assets")
