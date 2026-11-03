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
Module with utility functions for purging expired datastore items.

On MongoDB a TTL index removes them, other backends need this.
"""

from __future__ import absolute_import

from st2common.persistence.keyvalue import KeyValuePair

__all__ = ["purge_expired_keys"]


def purge_expired_keys(logger, timestamp):
    """
    :param timestamp: Items with an expire_timestamp before this timestamp will be deleted.
    :type timestamp: ``datetime.datetime``
    """
    if not timestamp:
        raise ValueError("Specify a valid timestamp to purge.")

    # Like the MongoDB TTL index, delete directly (no triggers are dispatched)
    deleted_count = KeyValuePair.delete_by_query(expire_timestamp__lt=timestamp)

    if deleted_count:
        logger.info("Deleted %s expired datastore items" % (deleted_count))

    return deleted_count
