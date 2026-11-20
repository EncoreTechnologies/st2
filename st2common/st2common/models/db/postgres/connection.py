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

from __future__ import absolute_import

import os

# psycopg's C implementation (psycopg[binary] / psycopg-c) waits for the server inside C code,
# which blocks the eventlet hub and so every green thread of the process. The pure python
# implementation waits through the (monkey patched) select module. Needs to be set before import.
os.environ["PSYCOPG_IMPL"] = "python"

import psycopg
import sqlalchemy as sa
from oslo_config import cfg

from st2common import log as logging

__all__ = [
    "connect",
    "disconnect",
    "get_engine",
    "is_connection_error",
]

LOG = logging.getLogger(__name__)

ENGINE = None


def check_psycopg_implementation():
    if psycopg.pq.__impl__ != "python":
        raise RuntimeError(
            'psycopg is using its "%s" implementation, which blocks eventlet. StackStorm needs '
            'the "python" implementation (install "psycopg" without the [binary] / [c] '
            "extras)." % (psycopg.pq.__impl__)
        )


def get_url(db_name, db_host, db_port, username=None, password=None):
    if "://" in db_host:
        url = sa.engine.make_url(db_host)

        if url.drivername in ["postgresql", "postgres"]:
            url = url.set(drivername="postgresql+psycopg")

        if not url.database:
            url = url.set(database=db_name)

        return url

    return sa.engine.URL.create(
        drivername="postgresql+psycopg",
        username=username,
        password=password,
        host=db_host,
        port=db_port,
        database=db_name,
    )


def connect(db_name, db_host, db_port, username=None, password=None):
    global ENGINE

    check_psycopg_implementation()

    url = get_url(db_name, db_host, db_port, username=username, password=password)
    connect_timeout = max(1, int(cfg.CONF.database.connection_timeout / 1000))

    LOG.info(
        'Connecting to database "%s" @ "%s:%s" as user "%s".',
        url.database,
        url.host,
        url.port,
        str(url.username),
    )

    engine = sa.create_engine(
        url, pool_pre_ping=True, connect_args={"connect_timeout": connect_timeout}
    )

    # Fail early (and get retried by the caller) when the server is not reachable
    with engine.connect() as conn:
        collation = conn.execute(
            sa.text(
                "SELECT datcollate FROM pg_database WHERE datname = current_database()"
            )
        ).scalar()

    if collation not in ["C", "POSIX"]:
        LOG.warning(
            'Database "%s" uses collation "%s". Use "C" so string sorting matches MongoDB '
            "(byte order).",
            url.database,
            collation,
        )

    ENGINE = engine
    return engine


def create_database(db_name, db_host, db_port, username=None, password=None):
    url = get_url(db_name, db_host, db_port, username=username, password=password)
    db_name = url.database

    engine = sa.create_engine(
        url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        with engine.connect() as conn:
            exists = conn.execute(
                sa.text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": db_name},
            ).scalar()
            if not exists:
                conn.execute(
                    sa.text(
                        "CREATE DATABASE \"%s\" TEMPLATE template0 LC_COLLATE 'C' "
                        "LC_CTYPE 'C' ENCODING 'UTF8'" % (db_name.replace('"', '""'))
                    )
                )
    finally:
        engine.dispose()


def disconnect():
    global ENGINE

    if ENGINE is not None:
        ENGINE.dispose()

    ENGINE = None


def get_engine():
    if ENGINE is None:
        raise RuntimeError("Not connected to the database, call db_setup() first.")

    return ENGINE


def is_connection_error(exc):
    return isinstance(
        exc,
        (
            sa.exc.OperationalError,
            sa.exc.InterfaceError,
            sa.exc.DisconnectionError,
            psycopg.OperationalError,
            psycopg.InterfaceError,
        ),
    )
