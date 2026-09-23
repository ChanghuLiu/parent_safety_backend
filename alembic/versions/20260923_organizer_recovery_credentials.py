"""add user-held organizer recovery credential metadata

Revision ID: 20260923_organizer_recovery_credentials
Revises: 20260922_public_invitation_codes
"""

from alembic import op
import sqlalchemy as sa

revision = "20260923_organizer_recovery_credentials"
down_revision = "20260922_public_invitation_codes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("users")}
    additions = (
        ("organizer_recovery_verifier", sa.String(length=64)),
        ("organizer_recovery_created_at", sa.DateTime()),
        ("organizer_recovery_used_at", sa.DateTime()),
        ("organizer_recovery_failed_attempts", sa.Integer(), {"nullable": False, "server_default": "0"}),
        ("organizer_recovery_locked_until", sa.DateTime()),
        ("organizer_recovery_one_time", sa.Boolean(), {"nullable": False, "server_default": "0"}),
    )
    for item in additions:
        name, column_type, *kwargs = item
        if name not in columns:
            op.add_column("users", sa.Column(name, column_type, **(kwargs[0] if kwargs else {})))
    op.create_index(
        "ix_users_organizer_recovery_verifier",
        "users",
        ["organizer_recovery_verifier"],
        unique=True,
        if_not_exists=True,
    )
    if "organizer_recovery_audit_events" not in inspector.get_table_names():
        op.create_table(
            "organizer_recovery_audit_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
            sa.Column("action", sa.String(length=64), nullable=False),
            sa.Column("source", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_organizer_recovery_audit_user_created", "organizer_recovery_audit_events", ["user_id", "created_at"])


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "organizer_recovery_audit_events" in inspector.get_table_names():
        op.drop_index("ix_organizer_recovery_audit_user_created", table_name="organizer_recovery_audit_events")
        op.drop_table("organizer_recovery_audit_events")
    op.drop_index("ix_users_organizer_recovery_verifier", table_name="users")
    for name in (
        "organizer_recovery_locked_until",
        "organizer_recovery_failed_attempts",
        "organizer_recovery_used_at",
        "organizer_recovery_created_at",
        "organizer_recovery_verifier",
        "organizer_recovery_one_time",
    ):
        op.drop_column("users", name)
