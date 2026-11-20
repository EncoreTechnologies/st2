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
Conversion between a document's stored (to_mongo()) form and a table row.

A row has:
- id: the ObjectId as a 24 character hex string
- doc (json): the stored form minus _id and minus JSONDictField values, exact (key order, any
  characters), encoded as MongoDB relaxed extended JSON so types round trip like BSON
- search (jsonb): copy of only the fields st2 filters / sorts on (see get_search_tree)
- one json column per JSONDictField (result, parameters, workflow state, ...): the orjson
  serialized value, uncompressed, stored once
"""

from __future__ import absolute_import

import calendar
import datetime
import json

import bson
from bson import json_util
from bson.binary import UuidRepresentation
from bson.json_util import JSONMode
from bson.json_util import JSONOptions
import mongoengine

from st2common.constants.compression import uncompress
from st2common.fields import JSONDictField

__all__ = [
    "decode_son",
    "encode_son",
    "get_blob_fields",
    "get_search_tree",
    "is_search_path",
    "is_scalar_field",
    "to_search_value",
]

JSON_OPTIONS = JSONOptions(
    json_mode=JSONMode.RELAXED,
    tz_aware=True,
    tzinfo=bson.tz_util.utc,
    uuid_representation=UuidRepresentation.PYTHON_LEGACY,
)

SCALAR_FIELD_TYPES = (
    mongoengine.StringField,
    mongoengine.IntField,
    mongoengine.LongField,
    mongoengine.FloatField,
    mongoengine.BooleanField,
    mongoengine.DateTimeField,
    mongoengine.ObjectIdField,
)

# Filtered on by st2 but neither a scalar field nor part of a model index
EXTRA_SEARCH_PATHS = {
    "LiveActionDB": ["context.paused_by"],
    "UserDB": ["nicknames"],
}

_CACHE = {}


def _cached(kind, model, func):
    key = (kind, model)
    if key not in _CACHE:
        _CACHE[key] = func(model)
    return _CACHE[key]


def is_scalar_field(field):
    return isinstance(field, SCALAR_FIELD_TYPES) and not isinstance(
        field, JSONDictField
    )


def get_blob_fields(model):
    """
    Return db field names of the model's JSONDictField fields (stored in their own columns).
    """

    def _get(model):
        return [
            field.db_field
            for field in model._fields.values()
            if isinstance(field, JSONDictField)
        ]

    return _cached("blobs", model, _get)


def get_search_tree(model):
    """
    Return the searchable paths of a model as a tree: {key: True (whole value) | {subtree}}.
    """

    def _get(model):
        paths = set()

        for field in model._fields.values():
            if field.db_field != "_id" and is_scalar_field(field):
                paths.add((field.db_field,))

        for spec in model._meta.get("index_specs", []):
            for key, _ in spec["fields"]:
                if key != "_id":
                    paths.add(tuple(key.split(".")))

        for path in EXTRA_SEARCH_PATHS.get(model.__name__, []):
            paths.add(tuple(path.split(".")))

        tree = {}
        for path in sorted(paths, key=len):
            node = tree
            for part in path[:-1]:
                node = node.setdefault(part, {})
                if node is True:
                    break
            else:
                node[path[-1]] = True

        return tree

    return _cached("search", model, _get)


def is_search_path(model, path):
    """
    True if the whole value at path is in the search copy.
    """
    node = get_search_tree(model)
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
        if node is True:
            return True
    return False


def to_search_value(value):
    """
    Convert a stored or query value to its search copy form.
    """
    if isinstance(value, datetime.datetime):
        # Millisecond precision, like a BSON date
        return calendar.timegm(value.utctimetuple()) * 1000 + value.microsecond // 1000
    if isinstance(value, bson.ObjectId):
        return str(value)
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {k: to_search_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_search_value(v) for v in value]
    if isinstance(value, bytes):
        return None
    return value


def _project(value, tree):
    if tree is True:
        return to_search_value(value)

    if isinstance(value, dict):
        result = {}
        for key, subtree in tree.items():
            if key in value:
                projected = _project(value[key], subtree)
                if projected is not _SKIP:
                    result[key] = projected
        return result

    if isinstance(value, (list, tuple)):
        return [_project(item, tree) for item in value if isinstance(item, dict)]

    return _SKIP


_SKIP = object()


def encode_son(model, son):
    """
    Return (id, doc json text, search json text, {blob column: json text or None}) for a
    to_mongo() style dict. son is not modified.
    """
    son = dict(son)
    doc_id = son.pop("_id", None)

    blobs = {}
    for name in get_blob_fields(model):
        value = son.pop(name, None)
        if value is None:
            blobs[name] = None
        elif isinstance(value, dict):
            # Old style native dict value
            blobs[name] = json.dumps(value)
        else:
            blobs[name] = uncompress(value).decode("utf-8")

    search = _project(son, get_search_tree(model))

    return (
        str(doc_id) if doc_id is not None else None,
        json_util.dumps(son, json_options=JSON_OPTIONS),
        json.dumps(search),
        blobs,
    )


def decode_son(model, doc_id, doc_text, blob_texts):
    """
    Build the to_mongo() style dict back from a row.
    """
    son = json_util.loads(doc_text, json_options=JSON_OPTIONS)
    son["_id"] = bson.ObjectId(doc_id)

    for name, text in blob_texts.items():
        if text is not None:
            son[name] = text.encode("utf-8")

    return son
