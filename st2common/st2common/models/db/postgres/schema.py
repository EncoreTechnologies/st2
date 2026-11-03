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
Table definitions generated from the mongoengine models (one table per collection).
"""

from __future__ import absolute_import

import re

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from st2common.models.db.postgres import codec

__all__ = ["METADATA", "get_table", "column_name", "search_expr", "create_tables"]

METADATA = sa.MetaData()

# Arbitrary, but fixed, key for the advisory lock held while creating tables. Every st2 service
# (and every sensor process) runs table creation on start.
SCHEMA_LOCK_KEY = 4208197301

_SAFE_KEY = re.compile(r"^[\w\-＄．]+$")


def column_name(field_name):
    return "f_%s" % (field_name)


def quote_key(key):
    if not _SAFE_KEY.match(key):
        raise ValueError('Unsupported field name "%s"' % (key))
    return "'%s'" % (key)


def search_expr(path, source="search"):
    """
    SQL expression for the jsonb value at path (a tuple of keys).
    """
    if len(path) == 1:
        return "(%s -> %s)" % (source, quote_key(path[0]))

    return "(%s #> '{%s}')" % (source, ",".join(quote_key(p)[1:-1] for p in path))


def _is_top_level_scalar(model, key):
    for field in model._fields.values():
        if field.db_field == key:
            return codec.is_scalar_field(field)
    return False


def _index_args(model, table_name, number, spec):
    keys = [key for key, _ in spec["fields"]]

    if "_id" in keys or not all(_is_top_level_scalar(model, key) for key in keys):
        # Nested / list paths are served by the GIN index on "search"
        return None

    name = "%s_i%s" % (table_name, number)
    unique = spec.get("unique", False)
    sparse = spec.get("sparse", False)

    exprs = []
    for key, direction in spec["fields"]:
        expr = search_expr((key,))
        if unique and not sparse:
            # MongoDB treats missing and null the same in a non sparse unique index
            expr = "COALESCE(%s, CAST('null' AS jsonb))" % (expr)
        if direction == -1:
            expr += " DESC"
        exprs.append(sa.text(expr))

    kwargs = {"unique": unique}
    if unique and sparse:
        kwargs["postgresql_where"] = sa.text(
            "(%s)" % " OR ".join("search ? %s" % quote_key(key) for key in keys)
        )

    return sa.Index(name, *exprs, **kwargs)


def get_table(model):
    table_name = model._get_collection_name()

    if table_name in METADATA.tables:
        return METADATA.tables[table_name]

    columns = [
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("seq", sa.BigInteger, sa.Identity(), nullable=False),
        sa.Column("doc", postgresql.JSON, nullable=False),
        sa.Column("search", postgresql.JSONB, nullable=False),
    ]
    for name in codec.get_blob_fields(model):
        columns.append(sa.Column(column_name(name), postgresql.JSON, nullable=True))

    indexes = [
        sa.Index("%s_seq" % (table_name), "seq"),
        sa.Index(
            "%s_search" % (table_name),
            "search",
            postgresql_using="gin",
            postgresql_ops={"search": "jsonb_path_ops"},
        ),
    ]
    for number, spec in enumerate(model._meta.get("index_specs", [])):
        index = _index_args(model, table_name, number, spec)
        if index is not None:
            indexes.append(index)

    return sa.Table(table_name, METADATA, *(columns + indexes))


def create_tables(engine, model_classes):
    tables = list({id(t): t for t in (get_table(m) for m in model_classes)}.values())

    with engine.begin() as conn:
        conn.execute(
            sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": SCHEMA_LOCK_KEY}
        )
        METADATA.create_all(conn, tables=tables, checkfirst=True)


def drop_tables(engine, model_classes):
    tables = list({id(t): t for t in (get_table(m) for m in model_classes)}.values())

    with engine.begin() as conn:
        conn.execute(
            sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": SCHEMA_LOCK_KEY}
        )
        METADATA.drop_all(conn, tables=tables, checkfirst=True)
