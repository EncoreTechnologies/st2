# Copyright 2020 The StackStorm Authors.
# Copyright 2019 Extreme Networks, Inc.
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

from st2common import transport
from st2common.models import db
from st2common.models.db import workflow as wf_db_models
from st2common.persistence import base as persistence


__all__ = ["ChangeRevisionResource", "WorkflowExecution", "TaskExecution"]


class ChangeRevisionResource(persistence.StatusBasedResource):
    @classmethod
    def update(
        cls, model_object, publish=True, dispatch_trigger=True, refresh=True, **kwargs
    ):
        # Writes here are compare-and-swap on rev and only take plain field values, so
        # there is no original state to read and roll back to like in the base update.
        model_object = cls._get_impl().update(model_object, **kwargs)

        if refresh:
            model_object = cls.get_by_id(model_object.id)

        if publish:
            cls.publish_update(model_object)

        if dispatch_trigger:
            cls.dispatch_update_trigger(model_object)

        return model_object


class WorkflowExecution(ChangeRevisionResource):
    impl = db.ChangeRevisionMongoDBAccess(wf_db_models.WorkflowExecutionDB)
    publisher = None

    @classmethod
    def _get_impl(cls):
        return cls.impl

    @classmethod
    def _get_publisher(cls):
        if not cls.publisher:
            cls.publisher = transport.workflow.WorkflowExecutionPublisher()

        return cls.publisher

    @classmethod
    def delete_by_query(cls, *args, **query):
        return cls._get_impl().delete_by_query(*args, **query)


class TaskExecution(ChangeRevisionResource):
    impl = db.ChangeRevisionMongoDBAccess(wf_db_models.TaskExecutionDB)
    publisher = None

    @classmethod
    def _get_impl(cls):
        return cls.impl

    @classmethod
    def delete_by_query(cls, *args, **query):
        return cls._get_impl().delete_by_query(*args, **query)
