"""Compatibility revision: original revision duplicated the baseline DDL.
Keep its ID so databases already stamped here remain upgradeable.
"""
revision = 'b1995c5a0eea'
down_revision = '3bce94637dbf'
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
