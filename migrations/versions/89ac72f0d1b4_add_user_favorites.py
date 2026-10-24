"""Add per-user file and folder favorites.

Revision ID: 89ac72f0d1b4
Revises: 45c071f381ae
Create Date: 2026-10-21
"""
from alembic import op
import sqlalchemy as sa


revision = "89ac72f0d1b4"
down_revision = "45c071f381ae"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "favorite",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("drive", sa.String(length=3), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("item_type", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "drive", "path", name="uq_favorite_user_drive_path"),
    )
    with op.batch_alter_table("favorite", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_favorite_user_id"), ["user_id"], unique=False)


def downgrade():
    with op.batch_alter_table("favorite", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_favorite_user_id"))
    op.drop_table("favorite")
