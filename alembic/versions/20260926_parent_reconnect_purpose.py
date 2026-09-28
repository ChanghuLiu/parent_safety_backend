"""persist explicit Parent invitation purpose for reconnect authorization"""

from alembic import op
import sqlalchemy as sa


revision = "20260926_parent_reconnect_purpose"
down_revision = "20260925_purchase_recovery_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("v2_family_invitations")}
    if "purpose" not in columns:
        op.add_column(
            "v2_family_invitations",
            sa.Column("purpose", sa.String(length=32), nullable=False, server_default="PARENT_CONNECT"),
        )
    op.create_index(
        "ix_v2_invitation_purpose",
        "v2_family_invitations",
        ["purpose"],
        if_not_exists=True,
    )
    op.execute(sa.text("""
        UPDATE v2_family_invitations
        SET purpose = 'PARENT_RECONNECT'
        WHERE status = 'pending'
          AND invited_role = 'PARENT'
          AND (
              SELECT COUNT(*)
              FROM v2_parent_profiles p
              WHERE p.family_circle_id = v2_family_invitations.family_circle_id
                AND p.active = 1
          ) = 1
    """))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "purpose" in {column["name"] for column in inspector.get_columns("v2_family_invitations")}:
        op.drop_index("ix_v2_invitation_purpose", table_name="v2_family_invitations")
        op.drop_column("v2_family_invitations", "purpose")
