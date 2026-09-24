"""Add viewer-specific family presentation metadata."""
from alembic import op
import sqlalchemy as sa

revision = "20260924_relationship_presentations"
down_revision = "20260924_profile_avatar"
branch_labels = None
depends_on = None

def upgrade():
    op.create_table(
        "v2_relationship_presentations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("family_circle_id", sa.Integer(), sa.ForeignKey("v2_family_circles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("viewer_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("subject_user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=True),
        sa.Column("avatar_url", sa.String(length=2048), nullable=True),
        sa.UniqueConstraint("family_circle_id", "viewer_user_id", "subject_user_id", name="uq_v2_relationship_presentation"),
    )
    op.create_index("ix_v2_relationship_presentations_circle", "v2_relationship_presentations", ["family_circle_id"])
    op.create_index("ix_v2_relationship_presentations_viewer", "v2_relationship_presentations", ["viewer_user_id"])
    op.create_index("ix_v2_relationship_presentations_subject", "v2_relationship_presentations", ["subject_user_id"])

def downgrade():
    op.drop_index("ix_v2_relationship_presentations_subject", table_name="v2_relationship_presentations")
    op.drop_index("ix_v2_relationship_presentations_viewer", table_name="v2_relationship_presentations")
    op.drop_index("ix_v2_relationship_presentations_circle", table_name="v2_relationship_presentations")
    op.drop_table("v2_relationship_presentations")
