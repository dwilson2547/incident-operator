"""Baseline: the schema as the startup DDL built it before Alembic (production was at exactly this, checked by a
schema dump diff on 2026-11-21). A database from before Alembic is stamped at this revision, not upgraded: see
docs/database.md.

Revision ID: 0001
Create Date: 2026-11-21
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('incidents',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('fingerprint', sa.Text(), nullable=False),
    sa.Column('alertname', sa.Text(), nullable=False),
    sa.Column('severity', sa.Text(), nullable=True),
    sa.Column('labels', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('annotations', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('generator_url', sa.Text(), nullable=True),
    sa.Column('status', sa.Text(), server_default='new', nullable=False),
    sa.Column('runbook', sa.Text(), nullable=True),
    sa.Column('starts_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('first_seen', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_seen', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('alert_resolved_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('analysis', sa.Text(), nullable=True),
    sa.Column('analysis_model', sa.Text(), nullable=True),
    sa.Column('analysis_worker', sa.Text(), nullable=True),
    sa.Column('notified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('problem_key', sa.Text(), nullable=True),
    sa.Column('repeat_of', sa.BigInteger(), nullable=True),
    sa.Column('runbook_rev', sa.Text(), nullable=True),
    sa.Column('target', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('findings', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('advice', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('attempt', sa.Integer(), server_default='1', nullable=False),
    sa.Column('rerun_model', sa.Text(), nullable=True),
    sa.Column('rerun_by', sa.Text(), nullable=True),
    sa.Column('earlier_attempts', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
    sa.CheckConstraint("status IN ('new', 'triaging', 'investigating', 'advising', 'analysed', 'triage_failed', 'repeat', 'skipped', 'rerun')", name='incidents_status_check'),
    sa.ForeignKeyConstraint(['repeat_of'], ['incidents.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('incidents_first_seen', 'incidents', [sa.literal_column('first_seen DESC')], unique=False)
    op.create_index('incidents_open_fingerprint', 'incidents', ['fingerprint'], unique=True, postgresql_where=sa.text('alert_resolved_at IS NULL'))
    op.create_index('incidents_problem_key', 'incidents', ['problem_key', sa.literal_column('first_seen DESC')], unique=False)
    op.create_index('incidents_repeat_of', 'incidents', ['repeat_of'], unique=False, postgresql_where=sa.text('repeat_of IS NOT NULL'))
    op.create_index('incidents_status', 'incidents', ['status'], unique=False, postgresql_where=sa.text("status IN ('new', 'triaging')"))
    op.create_table('library_last_good',
    sa.Column('file', sa.Text(), nullable=False),
    sa.Column('content', sa.LargeBinary(), nullable=False),
    sa.Column('rev', sa.Text(), nullable=False),
    sa.Column('saved_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('file')
    )
    op.create_table('evidence',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('incident_id', sa.BigInteger(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('source', sa.Text(), nullable=False),
    sa.Column('content', sa.Text(), nullable=True),
    sa.Column('truncated', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('collected_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('trigger', sa.Text(), server_default='generic', nullable=False),
    sa.Column('operation', sa.Text(), nullable=True),
    sa.Column('round', sa.Integer(), nullable=True),
    sa.Column('data', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.ForeignKeyConstraint(['incident_id'], ['incidents.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('evidence_incident', 'evidence', ['incident_id'], unique=False)
    op.create_table('model_turns',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('incident_id', sa.BigInteger(), nullable=False),
    sa.Column('session', sa.Text(), nullable=False),
    sa.Column('prompt_version', sa.Text(), nullable=False),
    sa.Column('model', sa.Text(), nullable=False),
    sa.Column('fallback', sa.ARRAY(sa.Text()), nullable=True),
    sa.Column('gpu_job_id', sa.Uuid(), nullable=True),
    sa.Column('status', sa.Text(), server_default='submitted', nullable=False),
    sa.Column('messages', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('ran_model', sa.Text(), nullable=True),
    sa.Column('worker', sa.Text(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('submitted_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('turn', sa.Integer(), server_default='1', nullable=False),
    sa.Column('json_schema', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('output', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('attempt', sa.Integer(), server_default='1', nullable=False),
    sa.CheckConstraint("status IN ('submitted', 'done', 'failed')", name='model_turns_status_check'),
    sa.ForeignKeyConstraint(['incident_id'], ['incidents.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('model_turns_incident', 'model_turns', ['incident_id'], unique=False)
    op.create_index('model_turns_pending', 'model_turns', ['status'], unique=False, postgresql_where=sa.text("status = 'submitted'"))
    op.create_table('op_runs',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('incident_id', sa.BigInteger(), nullable=False),
    sa.Column('operation', sa.Text(), nullable=False),
    sa.Column('op_rev', sa.Text(), nullable=False),
    sa.Column('params', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('autonomy', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('command', sa.Text(), nullable=True),
    sa.Column('proposed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('decided_by', sa.Text(), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('post_due_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('checks', sa.Text(), nullable=True),
    sa.Column('result', sa.Text(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('notified', sa.Text(), nullable=True),
    sa.CheckConstraint("status IN ('suggested', 'proposed', 'approved', 'rejected', 'refused', 'ready', 'running', 'settling', 'failed', 'succeeded', 'post_failed', 'superseded')", name='op_runs_status_check'),
    sa.ForeignKeyConstraint(['incident_id'], ['incidents.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('op_runs_active', 'op_runs', ['status'], unique=False, postgresql_where=sa.text("status IN ('proposed', 'approved', 'ready', 'running', 'settling')"))
    op.create_index('op_runs_incident', 'op_runs', ['incident_id'], unique=False)


def downgrade() -> None:
    for table in ("op_runs", "model_turns", "evidence", "library_last_good", "incidents"):
        op.drop_table(table)
