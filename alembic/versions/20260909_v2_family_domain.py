"""create Parent Check-In V2 family domain tables

Revision ID: 20260909_v2_family_domain
Revises:
"""

from alembic import op
import sqlalchemy as sa

import models  # noqa: F401
import v2_models


revision = "20260909_v2_family_domain"
down_revision = None
branch_labels = None
depends_on = None


V2_TABLES = [
    v2_models.FamilyCircle.__table__,
    v2_models.PurchaseEntitlement.__table__,
    v2_models.BillingRtdnEvent.__table__,
    v2_models.FamilyMembership.__table__,
    v2_models.ParentProfile.__table__,
    v2_models.CheckInSchedule.__table__,
    v2_models.CheckInEvent.__table__,
    v2_models.FamilyInvitation.__table__,
    v2_models.EscalationRule.__table__,
    v2_models.V2HelpRequest.__table__,
    v2_models.V2NotificationDelivery.__table__,
]


def upgrade() -> None:
    # The revision is additive and assumes the existing users identity table.
    # It never drops or rewrites legacy test data.
    models.User.__table__.create(op.get_bind(), checkfirst=True)
    inspector = sa.inspect(op.get_bind())
    if "users" in inspector.get_table_names() and "locale_tag" not in {c["name"] for c in inspector.get_columns("users")}:
        op.add_column("users", sa.Column("locale_tag", sa.String(length=32), nullable=True))
    for table in V2_TABLES:
        table.create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    for table in reversed(V2_TABLES):
        table.drop(op.get_bind(), checkfirst=True)
    inspector = sa.inspect(op.get_bind())
    if "users" in inspector.get_table_names() and "locale_tag" in {c["name"] for c in inspector.get_columns("users")}:
        op.drop_column("users", "locale_tag")
