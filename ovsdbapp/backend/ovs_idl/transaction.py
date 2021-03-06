# Copyright (c) 2017 Red Hat Inc
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

import logging
import time

from ovs.db import idl
from six.moves import queue as Queue

from ovsdbapp import api
from ovsdbapp.backend.ovs_idl import idlutils
from ovsdbapp import exceptions

LOG = logging.getLogger(__name__)


class Transaction(api.Transaction):
    def __init__(self, api, ovsdb_connection, timeout=None,
                 check_error=False, log_errors=True):
        self.api = api
        self.check_error = check_error
        self.log_errors = log_errors
        self.commands = []
        self.results = Queue.Queue(1)
        self.ovsdb_connection = ovsdb_connection
        self.timeout = timeout or ovsdb_connection.timeout

    def __str__(self):
        return ", ".join(str(cmd) for cmd in self.commands)

    def add(self, command):
        """Add a command to the transaction

        returns The command passed as a convenience
        """

        self.commands.append(command)
        return command

    def commit(self):
        self.ovsdb_connection.queue_txn(self)
        try:
            result = self.results.get(timeout=self.timeout)
        except Queue.Empty:
            raise exceptions.TimeoutException(commands=self.commands,
                                              timeout=self.timeout)
        if isinstance(result, idlutils.ExceptionResult):
            if self.log_errors:
                LOG.error(result.tb)
            if self.check_error:
                raise result.ex
        return result

    def pre_commit(self, txn):
        pass

    def post_commit(self, txn):
        for command in self.commands:
            command.post_commit(txn)

    def do_commit(self):
        self.start_time = time.time()
        attempts = 0
        while True:
            if attempts > 0 and self.timeout_exceeded():
                raise RuntimeError("OVS transaction timed out")
            attempts += 1
            # TODO(twilson) Make sure we don't loop longer than vsctl_timeout
            txn = idl.Transaction(self.api.idl)
            self.pre_commit(txn)
            for i, command in enumerate(self.commands):
                LOG.debug("Running txn command(idx=%(idx)s): %(cmd)s",
                          {'idx': i, 'cmd': command})
                try:
                    command.run_idl(txn)
                except Exception:
                    txn.abort()
                    if self.check_error:
                        raise
            seqno = self.api.idl.change_seqno
            status = txn.commit_block()
            if status == txn.TRY_AGAIN:
                LOG.debug("OVSDB transaction returned TRY_AGAIN, retrying")
                idlutils.wait_for_change(self.api.idl, self.time_remaining(),
                                         seqno)
                continue
            elif status == txn.ERROR:
                msg = "OVSDB Error: %s" % txn.get_error()
                if self.log_errors:
                    LOG.error(msg)
                if self.check_error:
                    # For now, raise similar error to vsctl/utils.execute()
                    raise RuntimeError(msg)
                return
            elif status == txn.ABORTED:
                LOG.debug("Transaction aborted")
                return
            elif status == txn.UNCHANGED:
                LOG.debug("Transaction caused no change")
            elif status == txn.SUCCESS:
                self.post_commit(txn)

            return [cmd.result for cmd in self.commands]

    def elapsed_time(self):
        return time.time() - self.start_time

    def time_remaining(self):
        return self.timeout - self.elapsed_time()

    def timeout_exceeded(self):
        return self.elapsed_time() > self.timeout
