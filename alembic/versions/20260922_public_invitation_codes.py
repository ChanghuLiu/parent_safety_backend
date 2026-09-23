"""add public invitation codes and redemption rate limiting

Revision ID: 20260922_public_invitation_codes
Revises: 20260916_parent_reinstall_recovery
"""

from alembic import op
import sqlalchemy as sa


revision = "20260922_public_invitation_codes"
down_revision = "20260916_parent_reinstall_recovery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    invitation_columns = {
        column["name"] for column in inspector.get_columns("v2_family_invitations")
    }
    if "public_code_hash" not in invitation_columns:
        op.add_column(
            "v2_family_invitations",
            sa.Column("public_code_hash", sa.String(length=64), nullable=True),
        )
    op.create_index(
        "uq_v2_invitation_public_code",
        "v2_family_invitations",
        ["public_code_hash"],
        unique=True,
        if_not_exists=True,
    )

    if "v2_invitation_attempts" not in inspector.get_table_names():
        op.create_table(
            "v2_invitation_attempts",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("attempted_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        )
        op.create_index(
            "ix_v2_invitation_attempts_attempted_at",
            "v2_invitation_attempts",
            ["attempted_at"],
        )
        op.create_index(
            "ix_v2_invitation_attempt_user_time",
            "v2_invitation_attempts",
            ["user_id", "attempted_at"],
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "v2_invitation_attempts" in inspector.get_table_names():
        op.drop_index(
            "ix_v2_invitation_attempt_user_time",
            table_name="v2_invitation_attempts",
        )
        op.drop_index(
            "ix_v2_invitation_attempts_attempted_at",
            table_name="v2_invitation_attempts",
        )
        op.drop_table("v2_invitation_attempts")
    invitation_columns = {
        column["name"] for column in inspector.get_columns("v2_family_invitations")
    }
    if "public_code_hash" in invitation_columns:
        op.drop_index(
            "uq_v2_invitation_public_code",
            table_name="v2_family_invitations",
        )
        op.drop_column("v2_family_invitations", "public_code_hash")
