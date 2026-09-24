"""add optional user avatar reference

Revision ID: 20260924_profile_avatar
Revises: 20260923_organizer_recovery_credentials
"""
from alembic import op
import sqlalchemy as sa

revision = "20260924_profile_avatar"
down_revision = "20260923_organizer_recovery_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("users")}
    if "avatar_url" not in columns:
        op.add_column("users", sa.Column("avatar_url", sa.String(length=2048), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "avatar_url" in {column["name"] for column in inspector.get_columns("users")}:
        op.drop_column("users", "avatar_url")
