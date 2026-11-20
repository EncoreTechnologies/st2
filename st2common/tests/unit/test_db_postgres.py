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
Storage behaviour of the postgresql backend which the MongoDB only tests cover for MongoDB.
"""

from __future__ import absolute_import

import datetime
import logging
import uuid

import bson
import eventlet

from st2common.garbage_collection.keyvalues import purge_expired_keys
from st2common.models.db.execution import ActionExecutionDB
from st2common.models.db.keyvalue import KeyValuePairDB
from st2common.models.db.liveaction import LiveActionDB
from st2common.persistence.execution import ActionExecution
from st2common.persistence.keyvalue import KeyValuePair
from st2common.persistence.liveaction import LiveAction
from st2common.util import date as date_utils
from st2tests import DbTestCase
from st2tests.base import get_stored_document
from st2tests.base import skip_unless_postgresql

LOG = logging.getLogger(__name__)


def _liveaction(**kwargs):
    values = {
        "action": "core.local",
        "status": "requested",
        "parameters": {},
        "result": {},
        "context": {"user": "stanley"},
        "start_timestamp": date_utils.get_datetime_utc_now(),
    }
    values.update(kwargs)
    return LiveActionDB(**values)


@skip_unless_postgresql
class PostgresStorageTestCase(DbTestCase):
    def test_big_field_keeps_key_order_and_null_characters(self):
        result = {"zeta": 1, "a": "x\x00y", "mid": [3, 2, 1]}
        liveaction_db = LiveAction.add_or_update(
            _liveaction(result=result), publish=False
        )

        retrieved = LiveAction.get_by_id(str(liveaction_db.id))
        self.assertEqual(list(retrieved.result.keys()), ["zeta", "a", "mid"])
        self.assertEqual(retrieved.result, result)

        # Stored once, as uncompressed JSON (Postgres compresses large values itself)
        stored = get_stored_document(LiveActionDB, liveaction_db.id)
        self.assertEqual(stored["result"], b'{"zeta":1,"a":"x\\u0000y","mid":[3,2,1]}')

    def test_record_field_keeps_null_characters(self):
        liveaction_db = LiveAction.add_or_update(
            _liveaction(context={"user": "stanley", "note": "a\x00b"}), publish=False
        )
        retrieved = LiveAction.get_by_id(str(liveaction_db.id))
        self.assertEqual(retrieved.context["note"], "a\x00b")

    def test_save_only_writes_changed_fields(self):
        liveaction_db = LiveAction.add_or_update(_liveaction(), publish=False)

        first = LiveAction.get_by_id(str(liveaction_db.id))
        second = LiveAction.get_by_id(str(liveaction_db.id))

        first.status = "running"
        LiveAction.add_or_update(first, publish=False)
        second.context = {"user": "other"}
        LiveAction.add_or_update(second, publish=False)

        retrieved = LiveAction.get_by_id(str(liveaction_db.id))
        self.assertEqual(retrieved.status, "running")
        self.assertEqual(retrieved.context, {"user": "other"})

    def test_concurrent_push_is_atomic(self):
        execution_db = ActionExecutionDB(
            action={"ref": "core.local"},
            runner={"name": "local-shell-cmd"},
            liveaction_id=uuid.uuid4().hex,
            status="running",
            children=[],
        )
        execution_db = ActionExecution.add_or_update(execution_db, publish=False)
        children = [str(bson.ObjectId()) for _ in range(20)]

        def push(child):
            ActionExecution.update(execution_db, publish=False, push__children=child)

        pool = eventlet.GreenPool(10)
        list(pool.imap(push, children))

        retrieved = ActionExecution.get_by_id(str(execution_db.id))
        self.assertCountEqual(retrieved.children, children)

    def test_expired_keys_are_purged(self):
        now = date_utils.get_datetime_utc_now()
        KeyValuePair.add_or_update(
            KeyValuePairDB(
                name="expired",
                value="v",
                expire_timestamp=now - datetime.timedelta(seconds=1),
            )
        )
        KeyValuePair.add_or_update(
            KeyValuePairDB(
                name="valid",
                value="v",
                expire_timestamp=now + datetime.timedelta(hours=1),
            )
        )
        KeyValuePair.add_or_update(KeyValuePairDB(name="no_expiry", value="v"))

        self.assertEqual(purge_expired_keys(logger=LOG, timestamp=now), 1)
        self.assertEqual(
            sorted(kvp.name for kvp in KeyValuePair.get_all()),
            ["no_expiry", "valid"],
        )

    def test_document_methods_use_the_backend(self):
        liveaction_db = _liveaction()
        liveaction_db.save()
        self.assertEqual(LiveActionDB.objects(id=liveaction_db.id).count(), 1)

        LiveAction.update(liveaction_db, publish=False, set__status="running")
        liveaction_db.reload()
        self.assertEqual(liveaction_db.status, "running")

        liveaction_db.delete()
        self.assertEqual(LiveActionDB.objects(id=liveaction_db.id).count(), 0)
