"""add secure Parent reinstall recovery metadata

Revision ID: 20260916_parent_reinstall_recovery
Revises: 20260909_v2_family_domain
"""

from alembic import op
import sqlalchemy as sa


revision = "20260916_parent_reinstall_recovery"
down_revision = "20260909_v2_family_domain"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    user_columns = {column["name"] for column in inspector.get_columns("users")}
    if "recovery_device_id" not in user_columns:
        op.add_column("users", sa.Column("recovery_device_id", sa.String(), nullable=True))
        op.create_index("ix_users_recovery_device_id", "users", ["recovery_device_id"])

    profile_columns = {column["name"] for column in inspector.get_columns("v2_parent_profiles")}
    if "recovered_from_parent_profile_id" not in profile_columns:
        op.add_column(
            "v2_parent_profiles",
            sa.Column("recovered_from_parent_profile_id", sa.Integer(), nullable=True),
        )
        op.create_index(
            "ix_v2_parent_profiles_recovered_from_parent_profile_id",
            "v2_parent_profiles",
            ["recovered_from_parent_profile_id"],
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    profile_columns = {column["name"] for column in inspector.get_columns("v2_parent_profiles")}
    if "recovered_from_parent_profile_id" in profile_columns:
        op.drop_index("ix_v2_parent_profiles_recovered_from_parent_profile_id", table_name="v2_parent_profiles")
        op.drop_column("v2_parent_profiles", "recovered_from_parent_profile_id")
    user_columns = {column["name"] for column in inspector.get_columns("users")}
    if "recovery_device_id" in user_columns:
        op.drop_index("ix_users_recovery_device_id", table_name="users")
        op.drop_column("users", "recovery_device_id")
