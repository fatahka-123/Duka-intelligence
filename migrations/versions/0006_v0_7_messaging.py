"""v0.7 messaging: per-shop & platform WhatsApp/SMS credentials, delivery tracking, opt-out

Revision ID: 0006
Revises: 0005
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0006'
down_revision: Union[str, Sequence[str], None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BIZ = [
    ('msg_source', sa.String(10), "platform"), ('wa_phone_id', sa.String(40), ""), ('wa_template', sa.String(80), ""),
    ('wa_template_lang', sa.String(10), "en"), ('at_username', sa.String(80), ""), ('at_sender_id', sa.String(20), ""),
    ('msg_hook_token', sa.String(64), ""),
]


def upgrade() -> None:
    with op.batch_alter_table('businesses') as t:
        for name, typ, default in BIZ:
            t.add_column(sa.Column(name, typ, server_default=default, nullable=False))
        for name in ('wa_token', 'wa_app_secret', 'at_api_key'):
            t.add_column(sa.Column(name, sa.Text(), server_default='', nullable=False))
        t.add_column(sa.Column('platform_messaging', sa.Boolean(), server_default=sa.true(), nullable=False))
        t.add_column(sa.Column('sms_fallback', sa.Boolean(), server_default=sa.true(), nullable=False))
    with op.batch_alter_table('customers') as t:
        t.add_column(sa.Column('marketing_opt_out', sa.Boolean(), server_default=sa.false(), nullable=False))
    with op.batch_alter_table('notifications') as t:
        t.add_column(sa.Column('provider_ref', sa.String(length=160), nullable=True))
        t.add_column(sa.Column('delivered_at', sa.DateTime(), nullable=True))
        t.add_column(sa.Column('source', sa.String(length=10), server_default='', nullable=False))
    op.create_index('ix_notifications_provider_ref', 'notifications', ['provider_ref'])
    op.create_table('platform_settings',
        sa.Column('key', sa.String(length=60), nullable=False),
        sa.Column('value', sa.Text(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('key'))


def downgrade() -> None:
    op.drop_table('platform_settings')
    op.drop_index('ix_notifications_provider_ref', 'notifications')
    with op.batch_alter_table('notifications') as t:
        for c in ('source', 'delivered_at', 'provider_ref'):
            t.drop_column(c)
    with op.batch_alter_table('customers') as t:
        t.drop_column('marketing_opt_out')
    with op.batch_alter_table('businesses') as t:
        for c in ['sms_fallback', 'platform_messaging', 'at_api_key', 'wa_app_secret', 'wa_token'] + [b[0] for b in reversed(BIZ)]:
            t.drop_column(c)
