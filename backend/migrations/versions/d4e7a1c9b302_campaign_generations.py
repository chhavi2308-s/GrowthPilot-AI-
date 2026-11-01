"""Research-driven campaign generation with per-day image status."""
import sqlalchemy as sa
from alembic import op

revision = "d4e7a1c9b302"
down_revision = "c92e81f4a073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "campaign_generations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("brand_id", sa.Integer(), sa.ForeignKey("brands.id", ondelete="CASCADE"), nullable=False),
        sa.Column("campaign_id", sa.Integer(), sa.ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("days", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(length=20), nullable=False),
        sa.Column("aspect_ratio", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("stage", sa.String(length=20), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("research", sa.JSON(), nullable=True),
        sa.Column("strategy", sa.JSON(), nullable=True),
        sa.Column("image_mode", sa.String(length=20), nullable=False),
        sa.Column("image_model", sa.String(length=80), nullable=False),
        sa.Column("budget_usd", sa.Double(), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_campaign_generations_workspace_id", "campaign_generations", ["workspace_id"])
    op.create_index("ix_campaign_generations_campaign_id", "campaign_generations", ["campaign_id"])
    op.create_index("ix_campaign_generations_status", "campaign_generations", ["status"])
    op.create_table(
        "campaign_generation_days",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("generation_id", sa.Integer(), sa.ForeignKey("campaign_generations.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("day", sa.Integer(), nullable=False),
        sa.Column("content_id", sa.Integer(), sa.ForeignKey("content_items.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("image_prompt", sa.JSON(), nullable=True),
        sa.Column("ai_asset_id", sa.Integer(), sa.ForeignKey("assets.id", ondelete="SET NULL"), nullable=True),
        sa.Column("asset_id", sa.Integer(), sa.ForeignKey("assets.id", ondelete="SET NULL"), nullable=True),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("generation_id", "day"),
    )
    op.create_index("ix_campaign_generation_days_generation_id", "campaign_generation_days", ["generation_id"])
    op.create_index("ix_campaign_generation_days_workspace_id", "campaign_generation_days", ["workspace_id"])
    op.create_index("ix_campaign_generation_days_status", "campaign_generation_days", ["status"])


def downgrade() -> None:
    op.drop_table("campaign_generation_days")
    op.drop_table("campaign_generations")
