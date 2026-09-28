"""durable relationship avatars and organizer Parent disconnect metadata"""

from alembic import op
import sqlalchemy as sa


revision = "20260928_disconnect_avatar"
down_revision = "20260926_parent_reconnect_purpose"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    presentation_columns = {
        column["name"]
        for column in inspector.get_columns("v2_relationship_presentations")
    }
    if "avatar_blob" not in presentation_columns:
        op.add_column(
            "v2_relationship_presentations",
            sa.Column("avatar_blob", sa.LargeBinary(), nullable=True),
        )
    if "avatar_mime_type" not in presentation_columns:
        op.add_column(
            "v2_relationship_presentations",
            sa.Column("avatar_mime_type", sa.String(length=64), nullable=True),
        )
    if "avatar_sha256" not in presentation_columns:
        op.add_column(
            "v2_relationship_presentations",
            sa.Column("avatar_sha256", sa.String(length=64), nullable=True),
        )

    invitation_columns = {
        column["name"]
        for column in inspector.get_columns("v2_family_invitations")
    }
    if "target_parent_profile_id" not in invitation_columns:
        op.add_column(
            "v2_family_invitations",
            sa.Column(
                "target_parent_profile_id",
                sa.Integer(),
                sa.ForeignKey("v2_parent_profiles.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
    op.create_index(
        "ix_v2_invitation_target_parent_profile",
        "v2_family_invitations",
        ["target_parent_profile_id"],
        if_not_exists=True,
    )

    tables = set(inspector.get_table_names())
    if "v2_parent_disconnect_audit_events" not in tables:
        op.create_table(
            "v2_parent_disconnect_audit_events",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "family_circle_id",
                sa.Integer(),
                sa.ForeignKey("v2_family_circles.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "organizer_user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column(
                "parent_user_id",
                sa.Integer(),
                sa.ForeignKey("users.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column(
                "parent_profile_id",
                sa.Integer(),
                sa.ForeignKey("v2_parent_profiles.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column("event_type", sa.String(length=32), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )
        op.create_index(
            "ix_v2_parent_disconnect_circle_created",
            "v2_parent_disconnect_audit_events",
            ["family_circle_id", "created_at"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "v2_parent_disconnect_audit_events" in set(inspector.get_table_names()):
        op.drop_index(
            "ix_v2_parent_disconnect_circle_created",
            table_name="v2_parent_disconnect_audit_events",
        )
        op.drop_table("v2_parent_disconnect_audit_events")

    invitation_columns = {
        column["name"]
        for column in inspector.get_columns("v2_family_invitations")
    }
    if "target_parent_profile_id" in invitation_columns:
        op.drop_index(
            "ix_v2_invitation_target_parent_profile",
            table_name="v2_family_invitations",
        )
        op.drop_column("v2_family_invitations", "target_parent_profile_id")

    presentation_columns = {
        column["name"]
        for column in inspector.get_columns("v2_relationship_presentations")
    }
    for name in ("avatar_sha256", "avatar_mime_type", "avatar_blob"):
        if name in presentation_columns:
            op.drop_column("v2_relationship_presentations", name)
