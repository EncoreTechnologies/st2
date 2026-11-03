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
PostgreSQL versions of MongoDBAccess / ChangeRevisionMongoDBAccess.

Reads reuse MongoDBAccess (incl. its null / datetime range filter processing) through
objects(). Writes mirror what mongoengine does on MongoDB: insert, replace-or-insert for a new
document with an id, changed fields only for a loaded document, atomic update operators. Every
write runs in one transaction with the row locked, so concurrent writers don't lose updates.
"""

from __future__ import absolute_import

import bson
import mongoengine
from mongoengine.queryset import transform
import sqlalchemy as sa

from st2common.exceptions import db as db_exc
from st2common.models.db import MongoDBAccess
from st2common.models.db.postgres import codec
from st2common.models.db.postgres import schema
from st2common.models.db.postgres.connection import get_engine
from st2common.models.db.postgres.query import PostgresQuerySet

__all__ = ["PostgresDBAccess", "ChangeRevisionPostgresDBAccess"]

UNIQUE_VIOLATION = "23505"


def _not_unique(func):
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except sa.exc.IntegrityError as e:
            if getattr(e.orig, "sqlstate", None) == UNIQUE_VIOLATION:
                # Same message format as mongoengine
                raise mongoengine.NotUniqueError(
                    "Tried to save duplicate unique keys (%s)" % (e.orig)
                )
            raise

    return wrapper


def _split_path(path):
    return [int(p) if p.isdigit() else p for p in path.split(".")]


def _get_parent(son, parts, create):
    node = son
    for part in parts[:-1]:
        if isinstance(node, list):
            node = node[part]
            continue
        if part not in node or node[part] is None:
            if not create:
                return None
            node[part] = {}
        node = node[part]
    return node


def _set(son, path, value):
    parts = _split_path(path)
    parent = _get_parent(son, parts, create=True)
    if isinstance(parent, list):
        parent[parts[-1]] = value
    else:
        parent[parts[-1]] = value


def _unset(son, path):
    parts = _split_path(path)
    parent = _get_parent(son, parts, create=False)
    if isinstance(parent, dict):
        parent.pop(parts[-1], None)
    elif isinstance(parent, list) and parts[-1] < len(parent):
        parent[parts[-1]] = None


def _get(son, path, default=None):
    parts = _split_path(path)
    parent = _get_parent(son, parts, create=False)
    if isinstance(parent, dict):
        return parent.get(parts[-1], default)
    return default


def apply_update(son, update):
    """
    Apply a MongoDB update document ($set, $unset, $inc, $push) to a to_mongo() style dict.
    """
    for op, values in update.items():
        for path, value in values.items():
            if op == "$set":
                _set(son, path, value)
            elif op == "$unset":
                _unset(son, path)
            elif op == "$inc":
                _set(son, path, _get(son, path, 0) + value)
            elif op == "$push":
                items = (
                    value["$each"]
                    if isinstance(value, dict) and "$each" in value
                    else [value]
                )
                current = _get(son, path, None)
                current = list(current) if current is not None else []
                _set(son, path, current + list(items))
            else:
                raise ValueError('Unsupported update operator "%s"' % (op))
    return son


class PostgresDBAccess(MongoDBAccess):
    """
    Like mongoengine, reads use the access object's model and writes use the document's own
    class (its table).
    """

    def _engine(self, model=None):
        engine = get_engine()
        schema.ensure_table(engine, model or self.model)
        return engine

    def objects(self, *args, **kwargs):
        self._engine()
        return PostgresQuerySet(self.model).filter(*args, **kwargs)

    def aggregate(self, *args, **kwargs):
        raise NotImplementedError("aggregate() is not supported with postgresql")

    # Row helpers

    def _insert_sql(self, model, blobs, replace=False):
        columns = ["id", "doc", "search"] + [schema.column_name(b) for b in blobs]
        casts = {"doc": "json", "search": "jsonb"}
        values = [
            "CAST(:%s AS %s)" % (c, casts.get(c, "json")) if c != "id" else ":id"
            for c in columns
        ]
        sql = "INSERT INTO %s (%s) VALUES (%s)" % (
            model._get_collection_name(),
            ", ".join(columns),
            ", ".join(values),
        )
        if replace:
            sql += " ON CONFLICT (id) DO UPDATE SET %s" % ", ".join(
                "%s = EXCLUDED.%s" % (c, c) for c in columns[1:]
            )
        return sql

    def _update_sql(self, model, blobs):
        sets = ["doc = CAST(:doc AS json)", "search = CAST(:search AS jsonb)"]
        sets += [
            "%s = CAST(:%s AS json)" % (schema.column_name(b), schema.column_name(b))
            for b in blobs
        ]
        return "UPDATE %s SET %s WHERE id = :id" % (
            model._get_collection_name(),
            ", ".join(sets),
        )

    def _write_son(self, conn, model, son, replace=False, update=False):
        doc_id, doc, search, blobs = codec.encode_son(model, son)
        values = {"id": doc_id, "doc": doc, "search": search}
        for name, value in blobs.items():
            values[schema.column_name(name)] = value

        if update:
            sql = self._update_sql(model, blobs)
        else:
            sql = self._insert_sql(model, blobs, replace=replace)
        conn.execute(sa.text(sql), values)

    def _lock_son(self, conn, model, doc_id):
        blobs = codec.get_blob_fields(model)
        columns = ["id", "CAST(doc AS text)"] + [
            "CAST(%s AS text)" % schema.column_name(b) for b in blobs
        ]
        row = conn.execute(
            sa.text(
                "SELECT %s FROM %s WHERE id = :id FOR UPDATE"
                % (", ".join(columns), model._get_collection_name())
            ),
            {"id": str(doc_id)},
        ).fetchone()
        if row is None:
            return None
        return codec.decode_son(model, row[0], row[1], dict(zip(blobs, row[2:])))

    def _saved(self, instance):
        instance._clear_changed_fields()
        instance._created = False
        return self._undo_dict_field_escape(instance)

    # Writes

    @_not_unique
    def insert(self, instance):
        model = type(instance)
        if instance.pk is None:
            instance.pk = bson.ObjectId()

        with self._engine(model).begin() as conn:
            self._write_son(conn, model, instance.to_mongo())

        return self._saved(instance)

    @_not_unique
    def add_or_update(self, instance, validate=True):
        model = type(instance)
        if validate:
            instance.validate()

        if instance.pk is None:
            instance.pk = bson.ObjectId()
            with self._engine(model).begin() as conn:
                self._write_son(conn, model, instance.to_mongo())
        elif instance._created:
            # New document with an id set: replace the existing one or insert (mongoengine)
            with self._engine(model).begin() as conn:
                self._write_son(conn, model, instance.to_mongo(), replace=True)
        else:
            self._save_changed_fields(instance)

        return self._saved(instance)

    def _save_changed_fields(self, instance, expected_rev=None):
        model = type(instance)
        update = instance._get_update_doc()
        if not update and expected_rev is None:
            return

        with self._engine(model).begin() as conn:
            son = self._lock_son(conn, model, instance.pk)

            if expected_rev is not None and (
                son is None or son.get("rev") != expected_rev
            ):
                raise db_exc.StackStormDBObjectWriteConflictError(instance)

            if son is None:
                # mongoengine saves changed fields with upsert
                son = apply_update({"_id": instance.pk}, update)
                self._write_son(conn, model, son)
                return

            self._write_son(conn, model, apply_update(son, update), update=True)

    @_not_unique
    def update(self, instance, **kwargs):
        model = type(instance)
        if instance.pk is None:
            raise mongoengine.OperationError(
                "attempt to update a document not yet saved"
            )
        if not kwargs:
            raise mongoengine.OperationError("No update parameters, would remove data")

        update = transform.update(model, **kwargs)

        with self._engine(model).begin() as conn:
            son = self._lock_son(conn, model, instance.pk)
            if son is None:
                return 0
            self._write_son(conn, model, apply_update(son, update), update=True)

        return 1

    def delete(self, instance):
        model = type(instance)
        with self._engine(model).begin() as conn:
            conn.execute(
                sa.text(
                    "DELETE FROM %s WHERE id = :id" % (model._get_collection_name())
                ),
                {"id": str(instance.pk)},
            )


class ChangeRevisionPostgresDBAccess(PostgresDBAccess):
    def add_or_update(self, instance, validate=True):
        return self.save(instance, validate=validate)

    def update(self, instance, **kwargs):
        for k, v in kwargs.items():
            setattr(instance, k, v)

        return self.save(instance)

    @_not_unique
    def save(self, instance, validate=True):
        if not getattr(instance, "id", None):
            return self.insert(instance)

        model = type(instance)
        expected_rev = instance.rev
        instance.rev = instance.rev + 1

        if validate:
            instance.validate()

        if instance._created:
            # mongoengine ignores the save condition for a new document
            with self._engine(model).begin() as conn:
                self._write_son(conn, model, instance.to_mongo(), replace=True)
        else:
            self._save_changed_fields(instance, expected_rev=expected_rev)

        return self._saved(instance)
