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
Translation of MongoDB style filter documents (as produced by mongoengine from st2's filter
kwargs and Q objects) to SQL, and a QuerySet-like result object.

Filters on fields in the "search" copy use it (and its indexes); any other path is evaluated on
the full document. Operators which aren't supported raise instead of returning a wrong result.
"""

from __future__ import absolute_import

import copy
import json
import re

import bson
from mongoengine.queryset.visitor import Q
import sqlalchemy as sa

from st2common.models.db.postgres import codec
from st2common.models.db.postgres import schema
from st2common.models.db.postgres.connection import get_engine

__all__ = ["PostgresQuerySet", "compile_filter", "compile_order_by"]

DOC_SOURCE = "CAST(doc AS jsonb)"
MAX_INT64 = 2**63 - 1
_STARTS_WITH = re.compile(r"^\^((?:\\.|[^\\.^$*+?{}\[\]|()])*)$")


class _Params(object):
    def __init__(self):
        self.values = {}

    def add(self, value):
        name = "p%s" % (len(self.values))
        self.values[name] = value
        return ":%s" % (name)


def _jp_path(path):
    return "$" + "".join("." + json.dumps(part) for part in path)


def _jp_literal(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    raise ValueError("Unsupported filter value: %r" % (value,))


def _is_top_level_scalar(model, path):
    if len(path) != 1:
        return False
    for field in model._fields.values():
        if field.db_field == path[0]:
            return codec.is_scalar_field(field)
    return False


class _FilterCompiler(object):
    def __init__(self, model, params):
        self.model = model
        self.params = params

    def compile(self, query):
        clauses = []
        for key, value in query.items():
            if key == "$and":
                clauses.append(
                    "(%s)" % " AND ".join(self.compile(q) or "TRUE" for q in value)
                )
            elif key == "$or":
                clauses.append(
                    "(%s)" % " OR ".join(self.compile(q) or "TRUE" for q in value)
                )
            elif key == "$nor":
                clauses.append(
                    "NOT (%s)" % " OR ".join(self.compile(q) or "TRUE" for q in value)
                )
            elif key.startswith("$"):
                raise ValueError('Unsupported filter operator "%s"' % (key))
            elif key == "_id":
                clauses.append(self._id_clause(value))
            else:
                clauses.append(self._field_clause(tuple(key.split(".")), value))

        return " AND ".join(clauses)

    # _id column

    def _id_value(self, value):
        if isinstance(value, bson.ObjectId):
            return str(value)
        return value

    def _id_clause(self, value):
        if isinstance(value, dict) and value and all(k.startswith("$") for k in value):
            clauses = []
            for op, operand in value.items():
                if op == "$in":
                    ids = [self._id_value(v) for v in operand]
                    clauses.append("id = ANY(%s)" % self.params.add(ids))
                elif op == "$nin":
                    ids = [self._id_value(v) for v in operand]
                    clauses.append("NOT (id = ANY(%s))" % self.params.add(ids))
                elif op == "$ne":
                    clauses.append(
                        "id <> %s" % self.params.add(self._id_value(operand))
                    )
                elif op in ("$lt", "$lte", "$gt", "$gte"):
                    sql_op = {"$lt": "<", "$lte": "<=", "$gt": ">", "$gte": ">="}[op]
                    clauses.append(
                        'id COLLATE "C" %s %s'
                        % (sql_op, self.params.add(self._id_value(operand)))
                    )
                elif op == "$exists":
                    clauses.append("TRUE" if operand else "FALSE")
                else:
                    raise ValueError('Unsupported filter operator "%s" on id' % (op))
            return "(%s)" % " AND ".join(clauses)

        if value is None:
            return "FALSE"

        return "id = %s" % self.params.add(self._id_value(value))

    # other fields

    def _source(self, path):
        if codec.is_search_path(self.model, path):
            return "search", codec.to_search_value
        return DOC_SOURCE, _doc_value

    def _field_clause(self, path, value):
        if isinstance(value, dict) and value and all(k.startswith("$") for k in value):
            return "(%s)" % " AND ".join(
                self._op_clause(path, op, operand) for op, operand in value.items()
            )
        if isinstance(value, re.Pattern):
            return self._regex_clause(path, value)
        return self._eq_clause(path, value)

    def _jp_exists(self, source, jsonpath):
        return "(%s @? CAST(%s AS jsonpath))" % (source, self.params.add(jsonpath))

    def _eq_clause(self, path, value):
        source, convert = self._source(path)
        value = convert(value)
        fast = source == "search" and _is_top_level_scalar(self.model, path)
        expr = schema.search_expr(path, source)

        if value is None:
            if fast:
                return "(%s IS NULL OR %s = CAST('null' AS jsonb))" % (expr, expr)
            return "(NOT %s OR %s)" % (
                self._jp_exists(source, _jp_path(path)),
                self._jp_exists(source, "%s ? (@ == null)" % _jp_path(path)),
            )

        if isinstance(value, (dict, list)):
            return "(%s = CAST(%s AS jsonb))" % (
                expr,
                self.params.add(json.dumps(value)),
            )

        if fast:
            return "(%s = CAST(%s AS jsonb))" % (
                expr,
                self.params.add(json.dumps(value)),
            )

        return self._jp_exists(
            source, "%s ? (@ == %s)" % (_jp_path(path), _jp_literal(value))
        )

    def _op_clause(self, path, op, operand):
        if op == "$in":
            if not operand:
                return "FALSE"
            return "(%s)" % " OR ".join(self._eq_clause(path, v) for v in operand)
        if op == "$nin":
            if not operand:
                return "TRUE"
            return "NOT (%s)" % " OR ".join(self._eq_clause(path, v) for v in operand)
        if op == "$ne":
            return "NOT %s" % (self._eq_clause(path, operand))
        if op == "$exists":
            source, _ = self._source(path)
            clause = self._jp_exists(source, _jp_path(path))
            return clause if operand else "NOT %s" % (clause)
        if op == "$not":
            if isinstance(operand, re.Pattern):
                return "NOT %s" % (self._regex_clause(path, operand))
            return "NOT %s" % (self._field_clause(path, operand))
        if op == "$regex":
            return self._regex_clause(path, re.compile(operand))
        if op in ("$lt", "$lte", "$gt", "$gte"):
            return self._range_clause(path, op, operand)
        raise ValueError('Unsupported filter operator "%s"' % (op))

    def _range_clause(self, path, op, operand):
        source, convert = self._source(path)
        value = convert(operand)
        sql_op = {"$lt": "<", "$lte": "<=", "$gt": ">", "$gte": ">="}[op]

        if source == "search" and _is_top_level_scalar(self.model, path):
            expr = schema.search_expr(path)
            param = self.params.add(json.dumps(value))
            # Like MongoDB, only compare values of the same type
            return (
                "(%s %s CAST(%s AS jsonb) AND jsonb_typeof(%s) = jsonb_typeof(CAST(%s AS jsonb)))"
                % (
                    expr,
                    sql_op,
                    param,
                    expr,
                    param,
                )
            )

        return self._jp_exists(
            source, "%s ? (@ %s %s)" % (_jp_path(path), sql_op, _jp_literal(value))
        )

    def _regex_clause(self, path, pattern):
        source, _ = self._source(path)
        match = _STARTS_WITH.match(pattern.pattern)

        if match and not pattern.flags & re.IGNORECASE:
            prefix = re.sub(r"\\(.)", r"\1", match.group(1))
            return self._jp_exists(
                source,
                "%s ? (@ starts with %s)" % (_jp_path(path), _jp_literal(prefix)),
            )

        flags = ' flag "i"' if pattern.flags & re.IGNORECASE else ""
        return self._jp_exists(
            source,
            "%s ? (@ like_regex %s%s)"
            % (_jp_path(path), _jp_literal(pattern.pattern), flags),
        )


def _doc_value(value):
    if isinstance(value, (dict, list)):
        return json.loads(bson.json_util.dumps(value, json_options=codec.JSON_OPTIONS))
    if isinstance(value, (bson.ObjectId, bytes)) or hasattr(value, "isoformat"):
        raise ValueError(
            "Filtering on non search fields with %s values is not supported"
            % type(value)
        )
    return value


def compile_filter(model, query, params):
    return _FilterCompiler(model, params).compile(query)


def _order_path(model, key):
    name = key.lstrip("+-")
    if name in ("id", "pk", "_id"):
        return None
    parts = name.replace("__", ".").split(".")
    field = model._fields.get(parts[0], None)
    if field is not None:
        parts[0] = field.db_field
    return tuple(parts)


def compile_order_by(model, order_by):
    items = []
    for key in order_by:
        desc = key.startswith("-")
        path = _order_path(model, key)
        if path is None:
            expr = 'id COLLATE "C"'
        elif codec.is_search_path(model, path):
            expr = schema.search_expr(path)
        else:
            expr = schema.search_expr(path, DOC_SOURCE)
        # MongoDB sorts missing values first when ascending and last when descending
        items.append("%s %s" % (expr, "DESC NULLS LAST" if desc else "ASC NULLS FIRST"))
    items.append("seq ASC")
    return ", ".join(items)


class PostgresQuerySet(object):
    """
    The subset of the mongoengine QuerySet API st2 uses on query results.
    """

    def __init__(self, model, query=None):
        self._model = model
        self._query = query or Q()
        self._order_by = []
        self._offset = 0
        self._limit = None
        self._only = None
        self._exclude = []
        self._cache = None
        self._use_cache = True

    def _clone(self):
        qs = copy.copy(self)
        qs._cache = None
        qs._exclude = list(self._exclude)
        qs._order_by = list(self._order_by)
        return qs

    clone = _clone

    # Filtering

    def filter(self, *q_objs, **query):
        qs = self._clone()
        new_query = Q(**query)
        for q_obj in q_objs:
            new_query &= q_obj
        qs._query = qs._query & new_query
        return qs

    __call__ = filter

    def _compiled_where(self):
        params = _Params()
        mongo_query = self._query.to_query(self._model)
        where = compile_filter(self._model, mongo_query, params) or "TRUE"
        return where, params.values

    # Modifiers

    def order_by(self, *keys):
        qs = self._clone()
        qs._order_by = [key for key in keys if key]
        return qs

    def limit(self, n):
        qs = self._clone()
        qs._limit = n
        return qs

    def skip(self, n):
        qs = self._clone()
        qs._offset = n
        return qs

    def _validate_fields(self, fields):
        for field in fields:
            path = field.replace("__", ".").split(".")
            self._model._lookup_field(path)

    def only(self, *fields):
        self._validate_fields(fields)
        qs = self._clone()
        qs._only = [field.replace("__", ".") for field in fields]
        return qs

    def exclude(self, *fields):
        self._validate_fields(fields)
        qs = self._clone()
        qs._exclude = qs._exclude + [field.replace("__", ".") for field in fields]
        return qs

    def no_dereference(self):
        return self

    def no_cache(self):
        qs = self._clone()
        qs._use_cache = False
        return qs

    # Evaluation

    def _db_field(self, name):
        field = self._model._fields.get(name, None)
        return field.db_field if field is not None else name

    def _blob_columns(self):
        blobs = codec.get_blob_fields(self._model)
        if self._only is not None:
            wanted = set(self._db_field(f.split(".")[0]) for f in self._only)
            blobs = [b for b in blobs if b in wanted]
        excluded = set(self._db_field(f) for f in self._exclude if "." not in f)
        return [b for b in blobs if b not in excluded]

    def _select_sql(self, offset, limit):
        # Same error as pymongo, callers turn it into a "too large" message
        for value in (offset, limit):
            if value is not None and value > MAX_INT64:
                raise OverflowError("Offset and limit must be 8-byte integers")

        table = self._model._get_collection_name()
        blobs = self._blob_columns()
        columns = ["id", "CAST(doc AS text)"] + [
            "CAST(%s AS text)" % schema.column_name(b) for b in blobs
        ]
        where, params = self._compiled_where()
        sql = "SELECT %s FROM %s WHERE %s ORDER BY %s" % (
            ", ".join(columns),
            table,
            where,
            compile_order_by(self._model, self._order_by),
        )
        if limit is not None:
            sql += " LIMIT %s" % (int(limit))
        if offset:
            sql += " OFFSET %s" % (int(offset))
        return sql, params, blobs

    def _trim(self, son):
        if self._only is not None:
            keep = set(self._db_field(f.split(".")[0]) for f in self._only)
            keep.add("_id")
            son = {k: v for k, v in son.items() if k in keep}

        for field in self._exclude:
            parts = field.split(".")
            parts[0] = self._db_field(parts[0])
            node = son
            for part in parts[:-1]:
                node = node.get(part, None) if isinstance(node, dict) else None
            if isinstance(node, dict):
                node.pop(parts[-1], None)

        return son

    def _fetch(self, offset, limit):
        sql, params, blobs = self._select_sql(offset, limit)
        with get_engine().connect() as conn:
            rows = conn.execute(sa.text(sql), params).fetchall()

        result = []
        for row in rows:
            son = codec.decode_son(
                self._model, row[0], row[1], dict(zip(blobs, row[2:]))
            )
            result.append(self._model._from_son(self._trim(son)))
        return result

    def _all(self):
        if self._cache is not None:
            return self._cache
        result = self._fetch(self._offset, self._limit)
        if self._use_cache:
            self._cache = result
        return result

    def __iter__(self):
        return iter(self._all())

    def __len__(self):
        return len(self._all())

    def __bool__(self):
        if self._cache is not None:
            return bool(self._cache)
        return bool(
            self._fetch(self._offset, 1 if self._limit is None else min(1, self._limit))
        )

    def __getitem__(self, key):
        if isinstance(key, slice):
            if key.step is not None or (key.start or 0) < 0:
                raise ValueError("Unsupported slice: %s" % (key))
            # Like mongoengine, a slice replaces any previous skip / limit
            qs = self._clone()
            qs._offset = key.start or 0
            qs._limit = key.stop
            if key.start and key.stop:
                qs._limit = max(0, key.stop - key.start)
            return qs

        if self._cache is not None:
            return self._cache[key]

        if key < 0 or (self._limit is not None and key >= self._limit):
            raise IndexError("list index out of range")

        result = self._fetch(self._offset + key, 1)
        if not result:
            raise IndexError("list index out of range")
        return result[0]

    def first(self):
        try:
            return self[0]
        except IndexError:
            return None

    def get(self, *q_objs, **query):
        qs = self.filter(*q_objs, **query)
        result = qs._fetch(qs._offset, 2)
        if not result:
            raise self._model.DoesNotExist(
                "%s matching query does not exist." % (self._model._class_name)
            )
        if len(result) > 1:
            raise self._model.MultipleObjectsReturned(
                "2 or more items returned, instead of 1"
            )
        return result[0]

    def count(self, with_limit_and_skip=False):
        if with_limit_and_skip and self._cache is not None:
            return len(self._cache)

        table = self._model._get_collection_name()
        where, params = self._compiled_where()
        sql = "SELECT count(*) FROM %s WHERE %s" % (table, where)
        if with_limit_and_skip and (self._offset or self._limit is not None):
            sql = "SELECT count(*) FROM (SELECT 1 FROM %s WHERE %s" % (table, where)
            if self._limit is not None:
                sql += " LIMIT %s" % (int(self._limit))
            sql += " OFFSET %s) AS page" % (int(self._offset))

        with get_engine().connect() as conn:
            return conn.execute(sa.text(sql), params).scalar()

    def scalar(self, *fields):
        result = []
        for doc in self.only(*fields):
            values = []
            for field in fields:
                value = doc
                for part in field.replace("__", ".").split("."):
                    value = getattr(value, part, None) if value is not None else None
                values.append(value)
            result.append(values[0] if len(fields) == 1 else tuple(values))
        return result

    def distinct(self, field):
        path = tuple(field.replace("__", ".").split("."))
        path = (self._db_field(path[0]),) + path[1:]
        table = self._model._get_collection_name()
        where, params = self._compiled_where()

        if path in (("_id",), ("id",)):
            sql = "SELECT DISTINCT id FROM %s WHERE %s" % (table, where)
            with get_engine().connect() as conn:
                return [bson.ObjectId(r[0]) for r in conn.execute(sa.text(sql), params)]

        source = "search" if codec.is_search_path(self._model, path) else DOC_SOURCE
        params["jp"] = _jp_path(path)
        sql = (
            "SELECT DISTINCT v FROM %s, jsonb_path_query(%s, CAST(:jp AS jsonpath)) AS v "
            "WHERE %s" % (table, source, where)
        )
        with get_engine().connect() as conn:
            return [r[0] for r in conn.execute(sa.text(sql), params)]

    def delete(self):
        table = self._model._get_collection_name()
        where, params = self._compiled_where()
        with get_engine().begin() as conn:
            return conn.execute(
                sa.text("DELETE FROM %s WHERE %s" % (table, where)), params
            ).rowcount
