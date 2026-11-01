"""Encrypted workspace-scoped OAuth credentials for Canva MCP."""
import sqlalchemy as sa
from alembic import op

revision = "c92e81f4a073"
down_revision = "a81d0a6f4a21"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("credentials_enc", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="authorized"),
        sa.Column("last_verified_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("workspace_id", "provider", name="uq_provider_credentials_workspace_provider"),
    )
    op.create_index("ix_provider_credentials_workspace_id", "provider_credentials", ["workspace_id"])


def downgrade() -> None:
    op.drop_index("ix_provider_credentials_workspace_id", table_name="provider_credentials")
    op.drop_table("provider_credentials")
