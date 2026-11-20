# Copyright 2020 The StackStorm Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
PostgreSQL database backend ([database] backend = postgresql).

Loaded by st2common.models.db only when that backend is configured.
"""

from __future__ import absolute_import

from oslo_config import cfg
import sqlalchemy as sa

from st2common import log as logging
from st2common.models.db.postgres import codec
from st2common.models.db.postgres import connection
from st2common.models.db.postgres import schema
from st2common.models.db.postgres.access import ChangeRevisionPostgresDBAccess
from st2common.models.db.postgres.access import PostgresDBAccess

__all__ = [
    "ACCESS_CLASSES",
    "create_database",
    "db_cleanup",
    "drop_collection",
    "get_stored_document",
    "db_ensure_indexes",
    "db_setup",
    "db_teardown",
    "is_connection_error",
]

LOG = logging.getLogger(__name__)

ACCESS_CLASSES = (PostgresDBAccess, ChangeRevisionPostgresDBAccess)

is_connection_error = connection.is_connection_error


def _check_rbac_backend():
    try:
        rbac_backend = cfg.CONF.rbac.backend
    except (cfg.NoSuchOptError, cfg.NoSuchGroupError):
        return

    if rbac_backend != "noop":
        raise ValueError(
            'RBAC backend "%s" is not supported with the postgresql database backend yet.'
            % (rbac_backend)
        )


def _get_model_classes():
    # Late import, st2common.models.db imports this module
    from st2common.models.db import get_model_classes

    return get_model_classes()


def get_stored_document(model, doc_id):
    """
    Return the stored (to_mongo() style) form of a document, or None. Used by the tests.
    """
    engine = connection.get_engine()
    schema.ensure_table(engine, model)
    blobs = codec.get_blob_fields(model)
    columns = ["id", "CAST(doc AS text)"] + [
        "CAST(%s AS text)" % schema.column_name(b) for b in blobs
    ]

    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT %s FROM %s WHERE id = :id"
                % (", ".join(columns), model._get_collection_name())
            ),
            {"id": str(doc_id)},
        ).fetchone()

    if row is None:
        return None
    return codec.decode_son(model, row[0], row[1], dict(zip(blobs, row[2:])))


def drop_collection(model):
    """
    Delete every document of a model (MongoDB drops the collection).
    """
    schema.truncate_tables(connection.get_engine(), [model])


class PostgresConnection(object):
    """
    Returned by db_setup() in place of the pymongo client.
    """

    def __init__(self, engine):
        self.engine = engine

    def drop_database(self, db_name):
        # Tables have to stay since they are not re-created on first use like MongoDB
        # collections, so "dropping" the database empties every StackStorm table
        schema.truncate_tables(self.engine, _get_model_classes())


def db_setup(
    db_name, db_host, db_port, username=None, password=None, ensure_indexes=True
):
    _check_rbac_backend()

    engine = connection.connect(
        db_name, db_host, db_port, username=username, password=password
    )

    schema.CREATED_TABLES.clear()

    # Tables must exist before first use (MongoDB creates collections and indexes lazily), so
    # they are always created, ensure_indexes or not
    db_ensure_indexes(_get_model_classes())

    return PostgresConnection(engine)


def create_database(db_name, db_host, db_port, username=None, password=None):
    """
    Create the database (C collation) if it doesn't exist yet. Used by the tests.
    """
    connection.create_database(
        db_name, db_host, db_port, username=username, password=password
    )


def db_ensure_indexes(model_classes=None):
    model_classes = model_classes or _get_model_classes()
    LOG.debug("Ensuring database tables and indexes...")
    schema.create_tables(connection.get_engine(), model_classes)


def db_teardown():
    connection.disconnect()


def db_cleanup(db_name, db_host, db_port, username=None, password=None):
    engine = connection.connect(
        db_name, db_host, db_port, username=username, password=password
    )

    LOG.info(
        'Dropping all StackStorm tables in database "%s" @ "%s:%s" as user "%s".',
        db_name,
        db_host,
        db_port,
        str(username),
    )

    schema.drop_tables(engine, _get_model_classes())
    return PostgresConnection(engine)
