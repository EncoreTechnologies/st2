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

from st2common import log as logging
from st2common.models.db.postgres import connection
from st2common.models.db.postgres import schema
from st2common.models.db.postgres.access import ChangeRevisionPostgresDBAccess
from st2common.models.db.postgres.access import PostgresDBAccess

__all__ = [
    "ACCESS_CLASSES",
    "db_cleanup",
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


def db_setup(
    db_name, db_host, db_port, username=None, password=None, ensure_indexes=True
):
    _check_rbac_backend()

    engine = connection.connect(
        db_name, db_host, db_port, username=username, password=password
    )

    # Tables must exist before first use (MongoDB creates collections and indexes lazily), so
    # they are always created, ensure_indexes or not
    db_ensure_indexes(_get_model_classes())

    return engine


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
    return engine
