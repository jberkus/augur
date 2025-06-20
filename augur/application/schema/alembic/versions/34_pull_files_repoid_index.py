"""Add index on FK pull_request_files.repo_id

Revision ID: 34
Revises: 33
Create Date: 2025-06-20

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from augur.application.db import create_database_engine, get_database_string


# revision identifiers, used by Alembic.
revision = '34'
down_revision = '33'
branch_labels = None
depends_on = None
def upgrade():
    op.create_index(
        'pull_request_files_repo_id',
        'pull_request_files',
        [ 'repo_id' ],
        schema = 'augur_data',
        if_not_exists = True
    )

def downgrade():
    op.drop_index(
        'pull_request_files_repo_id',
        'pull_request_files',
        schema = 'augur_data',
        if_exists = True
    )
