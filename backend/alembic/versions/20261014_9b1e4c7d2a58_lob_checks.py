"""lob checks

Revision ID: 9b1e4c7d2a58
Revises: 4ee0b53a79e6
Create Date: 2026-10-14 10:12:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
revision: str = '9b1e4c7d2a58'
down_revision: str | None = '4ee0b53a79e6'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql')


def upgrade() -> None:
    op.create_table('lob_checks',
    sa.Column('oracle_connection_id', sa.Uuid(), nullable=False),
    sa.Column('postgres_connection_id', sa.Uuid(), nullable=False),
    sa.Column('job_id', sa.Uuid(), nullable=True),
    sa.Column('status', sa.Enum('queued', 'running', 'succeeded', 'failed', name='scanstatus', native_enum=False, length=32), nullable=False),
    sa.Column('options', JSON, nullable=False),
    sa.Column('result', JSON, nullable=False),
    sa.Column('summary', JSON, nullable=False),
    sa.Column('warnings', JSON, nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], name=op.f('fk_lob_checks_job_id_jobs'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['oracle_connection_id'], ['connections.id'], name=op.f('fk_lob_checks_oracle_connection_id_connections'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['postgres_connection_id'], ['connections.id'], name=op.f('fk_lob_checks_postgres_connection_id_connections'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lob_checks'))
    )
    op.create_index(op.f('ix_lob_checks_oracle_connection_id'), 'lob_checks', ['oracle_connection_id'], unique=False)
    op.create_index(op.f('ix_lob_checks_postgres_connection_id'), 'lob_checks', ['postgres_connection_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_lob_checks_postgres_connection_id'), table_name='lob_checks')
    op.drop_index(op.f('ix_lob_checks_oracle_connection_id'), table_name='lob_checks')
    op.drop_table('lob_checks')
