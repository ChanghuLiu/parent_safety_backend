"""Add non-secret audit/rate-limit records for Play recovery attempts."""
from alembic import op
import sqlalchemy as sa

revision = "20260925_purchase_recovery_attempts"
down_revision = "20260924_relationship_presentations"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "organizer_purchase_recovery_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("device_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("purchase_token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("attempted_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_purchase_recovery_attempt_device_time",
        "organizer_purchase_recovery_attempts",
        ["device_fingerprint", "attempted_at"],
    )
    op.create_index(
        "ix_purchase_recovery_attempt_token_time",
        "organizer_purchase_recovery_attempts",
        ["purchase_token_hash", "attempted_at"],
    )
    op.create_index(
        "ix_organizer_purchase_recovery_attempts_user_id",
        "organizer_purchase_recovery_attempts",
        ["user_id"],
    )


def downgrade():
    op.drop_index("ix_organizer_purchase_recovery_attempts_user_id", table_name="organizer_purchase_recovery_attempts")
    op.drop_index("ix_purchase_recovery_attempt_token_time", table_name="organizer_purchase_recovery_attempts")
    op.drop_index("ix_purchase_recovery_attempt_device_time", table_name="organizer_purchase_recovery_attempts")
    op.drop_table("organizer_purchase_recovery_attempts")
