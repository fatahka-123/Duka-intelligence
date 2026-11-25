"""v0.4 purchase orders, password reset codes, staff phone

Revision ID: 0004
Revises: 0003
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('users') as t:
        t.add_column(sa.Column('phone', sa.String(length=40), server_default='', nullable=False))
        t.add_column(sa.Column('reset_code_hash', sa.String(length=256), server_default='', nullable=False))
        t.add_column(sa.Column('reset_expires_at', sa.DateTime(), nullable=True))
        t.add_column(sa.Column('reset_attempts', sa.Integer(), server_default='0', nullable=False))
    op.create_table('purchase_orders',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('business_id', sa.Integer(), sa.ForeignKey('businesses.id'), nullable=False),
        sa.Column('supplier_id', sa.Integer(), sa.ForeignKey('suppliers.id'), nullable=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('reference', sa.String(length=30), nullable=False),
        sa.Column('status', sa.String(length=12), nullable=False),
        sa.Column('notes', sa.String(length=240), server_default='', nullable=False),
        sa.Column('total', sa.Numeric(14, 2), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('sent_at', sa.DateTime(), nullable=True),
        sa.Column('received_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'))
    op.create_index('ix_purchase_orders_business_id', 'purchase_orders', ['business_id'])
    op.create_index('ix_purchase_orders_created_at', 'purchase_orders', ['created_at'])
    op.create_table('purchase_order_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('order_id', sa.Integer(), sa.ForeignKey('purchase_orders.id'), nullable=False),
        sa.Column('product_id', sa.Integer(), sa.ForeignKey('products.id'), nullable=False),
        sa.Column('product_name', sa.String(length=160), nullable=False),
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('unit_cost', sa.Numeric(14, 2), nullable=False),
        sa.Column('received_qty', sa.Integer(), server_default='0', nullable=False),
        sa.PrimaryKeyConstraint('id'))
    op.create_index('ix_purchase_order_items_order_id', 'purchase_order_items', ['order_id'])


def downgrade() -> None:
    op.drop_index('ix_purchase_order_items_order_id', 'purchase_order_items')
    op.drop_table('purchase_order_items')
    op.drop_index('ix_purchase_orders_created_at', 'purchase_orders')
    op.drop_index('ix_purchase_orders_business_id', 'purchase_orders')
    op.drop_table('purchase_orders')
    with op.batch_alter_table('users') as t:
        for c in ('reset_attempts', 'reset_expires_at', 'reset_code_hash', 'phone'):
            t.drop_column(c)
