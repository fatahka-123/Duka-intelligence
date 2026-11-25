"""v0.5 platform admin: pause shops, force password change

Revision ID: 0005
Revises: 0004
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = '0005'
down_revision: Union[str, Sequence[str], None] = '0004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('businesses') as t:
        t.add_column(sa.Column('active', sa.Boolean(), server_default=sa.true(), nullable=False))
        t.add_column(sa.Column('admin_note', sa.String(length=240), server_default='', nullable=False))
    with op.batch_alter_table('users') as t:
        t.add_column(sa.Column('must_change_password', sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('users') as t:
        t.drop_column('must_change_password')
    with op.batch_alter_table('businesses') as t:
        t.drop_column('admin_note')
        t.drop_column('active')
