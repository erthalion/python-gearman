import logging
import random
import sys

from gearman._connection_manager import GearmanConnectionManager
from gearman.errors import ConnectionError
from gearman.worker_handler import GearmanWorkerCommandHandler

gearman_logger = logging.getLogger('gearman.worker')

POLL_TIMEOUT_IN_SECONDS = 60.0

class GearmanWorker(GearmanConnectionManager):
    """GearmanWorkers manage connections and CommandHandlers

    This is the public facing gearman interface that most users should be instantiating
    All I/O will be handled by the GearmanWorker
    All state machine operations are handled on the CommandHandler
    """
    command_handler_class = GearmanWorkerCommandHandler

    def __init__(self, *args, **kwargs):
        # By default we should have non-blocking sockets for a GearmanWorker
        kwargs.setdefault('blocking_timeout', 0.0)
        super(GearmanWorker, self).__init__(*args, **kwargs)

        self.randomized_connections = None

        self.worker_abilities = {}
        self.worker_client_id = None
        self.command_handler_holding_job_lock = None

        self._update_initial_state()

    def _update_initial_state(self):
        self.handler_initial_state['abilities'] = self.worker_abilities.keys()
        self.handler_initial_state['client_id'] = self.worker_client_id

    def register_task(self, task, callback_function):
        """Register a function with gearman"""
        self.worker_abilities[task] = callback_function
        self._update_initial_state()

        for command_handler in self.handler_to_connection_map.iterkeys():
            command_handler.set_abilities(self.handler_initial_state['abilities'])

        return task

    def unregister_task(self, task):
        """Unregister a function with gearman"""
        self.worker_abilities.pop(task, None)
        self._update_initial_state()

        for command_handler in self.handler_to_connection_map.iterkeys():
            command_handler.set_abilities(self.handler_initial_state['abilities'])

        return task

    def set_client_id(self, client_id):
        self.worker_client_id = client_id
        self._update_initial_state()

        for command_handler in self.handler_to_connection_map.iterkeys():
            command_handler.set_client_id(self.handler_initial_state['client_id'])

        return client_id

    def get_worker_connections(self):
        """Return a shuffled list of connections that are alive,
        and try to reconnect to dead connections if necessary."""
        self.randomized_connections = list(self.connection_list)
        random.shuffle(self.randomized_connections)

        output_connections = []
        for current_connection in self.randomized_connections:
            self.attempt_connect(current_connection)
            if current_connection.is_connected():
                output_connections.append(current_connection)

        return output_connections

    def create_job(self, command_handler, job_handle, task, unique, data):
        current_connection = self.handler_to_connection_map[command_handler]
        return self.job_class(current_connection, job_handle, task, unique, data)

    def on_job_execute(self, current_job):
        try:
            function_callback = self.worker_abilities[current_job.task]
            job_result = function_callback(current_job)
        except Exception:
            return self.on_job_exception(current_job, sys.exc_info())

        return self.on_job_complete(current_job, job_result)

    def on_job_exception(self, current_job, exc_info):
        self.send_job_failure(current_job)
        return False

    def on_job_complete(self, current_job, job_result):
        self.send_job_complete(current_job, job_result)
        return True

    def set_job_lock(self, command_handler, lock):
        if command_handler not in self.handler_to_connection_map:
            return False

        failed_lock = bool(lock and self.command_handler_holding_job_lock is not None)
        failed_unlock = bool(not lock and self.command_handler_holding_job_lock != command_handler)

        # If we've already been locked, we should say the lock failed
        # If we're attempting to unlock something when we don't have a lock, we're in a bad state
        if failed_lock or failed_unlock:
            return False

        if lock:
            self.command_handler_holding_job_lock = command_handler
        elif not lock:
            self.command_handler_holding_job_lock = None

        return True

    def check_job_lock(self, command_handler):
        return bool(self.command_handler_holding_job_lock == command_handler)

    # Send Gearman commands related to jobs
    def _get_handler_for_job(self, current_job):
        return self.connection_to_handler_map[current_job.conn]

    def send_job_status(self, current_job, numerator, denominator):
        current_handler = self._get_handler_for_job(current_job)
        current_handler.send_job_status(current_job, numerator=numerator, denominator=denominator)

    def send_job_complete(self, current_job, data):
        current_handler = self._get_handler_for_job(current_job)
        current_handler.send_job_complete(current_job, data=data)

    def send_job_failure(self, current_job):
        """Removes a job from the queue if its backgrounded"""
        current_handler = self._get_handler_for_job(current_job)
        current_handler.send_job_failure(current_job)

    def send_job_exception(self, current_job, data):
        """Removes a job from the queue if its backgrounded"""
        # Using GEARMAND_COMMAND_WORK_EXCEPTION is not recommended at time of this writing [2010-02-24]
        # http://groups.google.com/group/gearman/browse_thread/thread/5c91acc31bd10688/529e586405ed37fe
        #
        current_handler = self._get_handler_for_job(current_job)
        current_handler.send_job_exception(current_job, data=data)
        current_handler.send_job_failure(current_job)

    def send_job_data(self, current_job, data):
        current_handler = self._get_handler_for_job(current_job)
        current_handler.send_job_data(current_job, data=data)

    def send_job_warning(self, current_job, data):
        current_handler = self._get_handler_for_job(current_job)
        current_handler.send_job_warning(current_job, data=data)

    def work(self, poll_timeout=POLL_TIMEOUT_IN_SECONDS):
        """Loop indefinitely working tasks from all connections."""
        continue_working = True
        live_connections = []

        def continue_while_connections_alive(any_activity):
            return self.after_poll(any_activity)

        # Shuffle our connections after the poll timeout
        while continue_working:
            worker_connections = self.get_worker_connections()
            continue_working = self.poll_connections_until_stopped(worker_connections, continue_while_connections_alive, timeout=poll_timeout)

        # If we were kicked out of the worker loop, we should shutdown all our connections
        for current_connection in live_connections:
            current_connection.close()

    def after_poll(self, any_activity):
        return True

    def shutdown(self):
        self.command_handler_holding_job_lock = None
        super(GearmanWorker, self).shutdown(current_connection)

    def handle_error(self, current_connection):
        current_handler = self.connection_to_handler_map.get(current_connection)
        if current_handler:
            self.set_job_lock(current_handler, lock=False)

        super(GearmanWorker, self).handle_error(current_connection)
