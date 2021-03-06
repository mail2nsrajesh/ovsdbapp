# Copyright (c) 2017 Red Hat Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import collections
import logging

import six

from ovsdbapp import api
from ovsdbapp.backend.ovs_idl import idlutils

LOG = logging.getLogger(__name__)


class BaseCommand(api.Command):
    def __init__(self, api):
        self.api = api
        self.result = None

    def execute(self, check_error=False, log_errors=True):
        try:
            with self.api.transaction(check_error, log_errors) as txn:
                txn.add(self)
            return self.result
        except Exception:
            if log_errors:
                LOG.exception("Error executing command")
            if check_error:
                raise

    def post_commit(self, txn):
        pass

    def __str__(self):
        command_info = self.__dict__
        return "%s(%s)" % (
            self.__class__.__name__,
            ", ".join("%s=%s" % (k, v) for k, v in command_info.items()
                      if k not in ['api', 'result']))


class DbCreateCommand(BaseCommand):
    def __init__(self, api, table, **columns):
        super(DbCreateCommand, self).__init__(api)
        self.table = table
        self.columns = columns

    def run_idl(self, txn):
        row = txn.insert(self.api._tables[self.table])
        for col, val in self.columns.items():
            setattr(row, col, idlutils.db_replace_record(val))
        # This is a temporary row to be used within the transaction
        self.result = row

    def post_commit(self, txn):
        # Replace the temporary row with the post-commit UUID to match vsctl
        self.result = txn.get_insert_uuid(self.result.uuid)


class DbDestroyCommand(BaseCommand):
    def __init__(self, api, table, record):
        super(DbDestroyCommand, self).__init__(api)
        self.table = table
        self.record = record

    def run_idl(self, txn):
        record = idlutils.row_by_record(self.api.idl, self.table, self.record)
        record.delete()


class DbSetCommand(BaseCommand):
    def __init__(self, api, table, record, *col_values):
        super(DbSetCommand, self).__init__(api)
        self.table = table
        self.record = record
        self.col_values = col_values

    def run_idl(self, txn):
        record = idlutils.row_by_record(self.api.idl, self.table, self.record)
        for col, val in self.col_values:
            # TODO(twilson) Ugh, the OVS library doesn't like OrderedDict
            # We're only using it to make a unit test work, so we should fix
            # this soon.
            if isinstance(val, collections.OrderedDict):
                val = dict(val)
            if isinstance(val, dict):
                # NOTE(twilson) OVS 2.6's Python IDL has mutate methods that
                # would make this cleaner, but it's too early to rely on them.
                existing = getattr(record, col, {})
                existing.update(val)
                val = existing
            setattr(record, col, idlutils.db_replace_record(val))


class DbAddCommand(BaseCommand):
    def __init__(self, api, table, record, column, *values):
        super(DbAddCommand, self).__init__(api)
        self.table = table
        self.record = record
        self.column = column
        self.values = values

    def run_idl(self, txn):
        record = idlutils.row_by_record(self.api.idl, self.table, self.record)
        for value in self.values:
            if isinstance(value, collections.Mapping):
                # We should be doing an add on a 'map' column. If the key is
                # already set, do nothing, otherwise set the key to the value
                # Since this operation depends on the previous value, verify()
                # must be called.
                field = getattr(record, self.column, {})
                for k, v in six.iteritems(value):
                    if k in field:
                        continue
                    field[k] = v
            else:
                # We should be appending to a 'set' column.
                try:
                    record.addvalue(self.column,
                                    idlutils.db_replace_record(value))
                    continue
                except AttributeError:  # OVS < 2.6
                    field = getattr(record, self.column, [])
                    field.append(value)
            record.verify(self.column)
            setattr(record, self.column, idlutils.db_replace_record(field))


class DbClearCommand(BaseCommand):
    def __init__(self, api, table, record, column):
        super(DbClearCommand, self).__init__(api)
        self.table = table
        self.record = record
        self.column = column

    def run_idl(self, txn):
        record = idlutils.row_by_record(self.api.idl, self.table, self.record)
        # Create an empty value of the column type
        value = type(getattr(record, self.column))()
        setattr(record, self.column, value)


class DbGetCommand(BaseCommand):
    def __init__(self, api, table, record, column):
        super(DbGetCommand, self).__init__(api)
        self.table = table
        self.record = record
        self.column = column

    def run_idl(self, txn):
        record = idlutils.row_by_record(self.api.idl, self.table, self.record)
        # TODO(twilson) This feels wrong, but ovs-vsctl returns single results
        # on set types without the list. The IDL is returning them as lists,
        # even if the set has the maximum number of items set to 1. Might be
        # able to inspect the Schema and just do this conversion for that case.
        result = idlutils.get_column_value(record, self.column)
        if isinstance(result, list) and len(result) == 1:
            self.result = result[0]
        else:
            self.result = result


class DbListCommand(BaseCommand):
    def __init__(self, api, table, records, columns, if_exists):
        super(DbListCommand, self).__init__(api)
        self.table = table
        self.columns = columns
        self.if_exists = if_exists
        self.records = records

    def run_idl(self, txn):
        table_schema = self.api._tables[self.table]
        columns = self.columns or list(table_schema.columns.keys()) + ['_uuid']
        if self.records:
            row_uuids = []
            for record in self.records:
                try:
                    row_uuids.append(idlutils.row_by_record(
                                     self.api.idl, self.table, record).uuid)
                except idlutils.RowNotFound:
                    if self.if_exists:
                        continue
                    # NOTE(kevinbenton): this is converted to a RuntimeError
                    # for compat with the vsctl version. It might make more
                    # sense to change this to a RowNotFoundError in the future.
                    raise RuntimeError(
                        "Row doesn't exist in the DB. Request info: "
                        "Table=%(table)s. Columns=%(columns)s. "
                        "Records=%(records)s." % {
                            "table": self.table,
                            "columns": self.columns,
                            "records": self.records})
        else:
            row_uuids = table_schema.rows.keys()
        self.result = [
            {
                c: idlutils.get_column_value(table_schema.rows[uuid], c)
                for c in columns
            }
            for uuid in row_uuids
        ]


class DbFindCommand(BaseCommand):
    def __init__(self, api, table, *conditions, **kwargs):
        super(DbFindCommand, self).__init__(api)
        self.table = self.api._tables[table]
        self.conditions = conditions
        self.columns = (kwargs.get('columns') or
                        list(self.table.columns.keys()) + ['_uuid'])

    def run_idl(self, txn):
        self.result = [
            {
                c: idlutils.get_column_value(r, c)
                for c in self.columns
            }
            for r in self.table.rows.values()
            if idlutils.row_match(r, self.conditions)
        ]
