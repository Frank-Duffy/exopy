# -*- coding: utf-8 -*-
# -----------------------------------------------------------------------------
# Copyright 2015 by Ecpy Authors, see AUTHORS for more details.
#
# Distributed under the terms of the BSD license.
#
# The full license is in the file LICENCE, distributed with this software.
# -----------------------------------------------------------------------------
"""Engine executing the measure in a different process.

"""
from __future__ import (division, unicode_literals, print_function,
                        absolute_import)

import logging
from multiprocessing import Pipe
from multiprocessing.queues import Queue
from multiprocessing.synchronize import Event
from threading import Thread
from threading import Event as tEvent

from atom.api import Typed, Value, Bool

from ....utils.log.tools import QueueLoggerThread
from ..base_engine import BaseEngine
from ..tools import ThreadMeasureMonitor
from .subprocess import TaskProcess

logger = logging.getLogger(__name__)


class ProcessEngine(BaseEngine):
    """An engine executing the tasks it is sent in a different process.

    """

    def perform(self, task_infos):
        """Execute a given task.

        Parameters
        ----------
        task_infos : TaskInfos
            TaskInfos object describing the work to expected of the engine.

        Returns
        -------
        task_infos : TaskInfos
            Input object whose values have been updated. This is simply a
            convenience.

        """
        self.status = 'Running'

        # Clear all the flags.
        self._task_pause.clear()
        self._task_paused.clear()
        self._task_resume.clear()
        self._task_stop.clear()
        self._force_stop.clear()
        self._stop_requested = False

        # If the process does not exist or is dead create a new one.
        if not self._process or not self._process.is_alive():

            self._process_stop.clear()

            # Create the subprocess and the pipe.
            self._pipe, process_pipe = Pipe()
            self._process = TaskProcess(process_pipe,
                                        self._log_queue,
                                        self._monitor_queue,
                                        self._task_pause,
                                        self._task_paused,
                                        self._task_stop,
                                        self._process_stop)
            self._process.daemon = True

            # Create the logger thread in charge of dispatching log reports.
            self._log_thread = QueueLoggerThread(self._log_queue)
            self._log_thread.daemon = True
            logger.debug('Starting logging thread.')
            self._log_thread.start()

            # Create the monitor thread dispatching engine news to the monitor.
            self._monitor_thread = ThreadMeasureMonitor(self,
                                                        self._monitor_queue)
            self._monitor_thread.daemon = True
            logger.debug('Starting monitoring thread.')
            self._monitor_thread.start()

            self._pause_thread = None

            # Start process.
            logger.debug('Starting subprocess')
            self._process.start()

        # Send the measure.
        task_infos.task.update_preferences_from_members()
        config = task_infos.task.preferences
        database_root_state = task_infos.task.database.copy_node_values()
        self._pipe.send((task_infos.id, config,
                         task_infos.build_deps,
                         task_infos.runtime_deps,
                         task_infos.observed_entries,
                         database_root_state
                         ))
        logger.debug('Task {} sent'.format(task_infos.id))

        # Check that the engine did receive the task.
        while not self._pipe.poll(2):
            if not self._process.is_alive():
                msg = 'Subprocess was found dead unexpectedly'
                logger.debug(msg)
                self._log_queue.put(None)
                self._monitor_queue.put((None, None))
                self._cleanup(process=False)
                task_infos.success = False
                task_infos.errors['engine'] = msg
                return task_infos

        status = self._pipe.recv()
        if not status:
            msg = "Subprocess can't perform the task."
            logger.debug(msg)
            self._cleanup()
            task_infos.success = False
            task_infos.errors['engine'] = msg
            return task_infos

        # Wait for the process to finish the measure and check it has not
        # been killed.
        while not self._pipe.poll(1):
            if self._force_stop.is_set():
                msg = 'Subprocess was terminated by the user.'
                logger.debug(msg)
                self._cleanup(process=False)
                task_infos.success = False
                task_infos.errors['engine'] = msg
                return task_infos

            # Here get message from process and react
            result, errors = self._pipe.recv()
            logger.debug('Subprocess done performing measure')

            task_infos.success = result
            task_infos.errors.update(errors)

        self.status = 'Waiting'

        return task_infos

    def pause(self):
        """Ask the engine to pause the current task execution.

        """
        self.status = 'Pausing'
        self._task_resume.clear()
        self._task_paused.clear()
        self._task_pause.set()

        self._pause_thread = Thread(target=self._wait_for_pause)
        self._pause_thread.start()

    def resume(self):
        """Ask the engine to resume the currently paused job.

        """
        self.status = 'Resuming'
        self._task_pause.clear()
        self._task_resume.set()

    def stop(self, force=False):
        """Ask the engine to stop the current job.

        This method should not wait for the job to stop save if a forced stop
        was requested.

        Parameters
        ----------
        force : bool, optional
            Force the engine to stop the performing the task. This allow the
            engine to use any means necessary to stop, in this case only should
            the call to this method block.

        """
        self._stop_requested = True
        self._task_stop.set()

        if force:
            self._force_stop.set()

            # Stop running queues
            self._log_queue.put(None)
            self._monitor_queue.put((None, None))

            # Terminate the process and make sure all threads stopped properly.
            self._process.terminate()
            self._log_thread.join()
            self._monitor_thread.join()

            # Discard the queues as they may have been corrupted when the
            # process was terminated.
            self._log_queue = Queue()
            self._monitor_queue = Queue()

            self.status = 'Stopped'

    def shutdown(self, force=False):
        """Ask the engine to stop completely.

        Parameters
        ----------
        force : bool, optional
            Force the engine to stop the performing the task. This allow the
            engine to use any means necessary to stop, in this case only should
            the call to this method block.

        """
        self._stop_requested = True
        self._task_stop.set()

        if not force:
            t = Thread(target=self._cleanup)
            t.start()

        else:
            self.stop(force=True)

    # =========================================================================
    # --- Private API ---------------------------------------------------------
    # =========================================================================

    #: Boolean indicating that the user requested the job to stop.
    _stop_requested = Bool()

    #: Interprocess event used to pause the subprocess current job.
    _task_pause = Typed(Event, ())

    #: Interprocess event signaling the subprocess current job is paused.
    _task_paused = Typed(Event, ())

    #: Interprocess event signaling the subprocess current job has resumed.
    _task_resumed = Typed(Event, ())

    #: Interprocess event used to stop the subprocess current measure.
    _task_stop = Typed(Event, ())

    #: Interprocess event used to stop the subprocess.
    _process_stop = Typed(Event, ())

    #: Flag signaling that a forced exit has been requested
    _force_stop = Value(tEvent())

    #: Current subprocess.
    _process = Typed(TaskProcess)

    #: Connection used to send and receive messages about execution (type
    #: ambiguous when the OS is not known)
    _pipe = Value()

    #: Inter-process queue used by the subprocess to transmit its log records.
    _log_queue = Typed(Queue, ())

    #: Thread in charge of collecting the log message coming from the
    #: subprocess.
    _log_thread = Typed(Thread)

    #: Inter-process queue used by the subprocess to send the values of the
    #: observed database entries.
    _monitor_queue = Typed(Queue, ())

    #: Thread in charge of collecting the values of the observed database
    #: entries.
    _monitor_thread = Typed(Thread)

    #: Thread in charge of notifying the engine that the engine did
    #: pause/resume after being asked to do so.
    _pause_thread = Typed(Thread)

    def _cleanup(self, process=True):
        """ Helper method taking care of making sure that everybody stops.

        Parameters
        ----------
        process : bool
            Whether to join the worker process. Used when the process has been
            termintaed abruptly.

        """
        logger.debug('Cleaning up')

        if process:
            self._process_stop.set()
            self._process.join()
            logger.debug('Subprocess joined')
        self._pipe.close()

        self._log_thread.join()
        logger.debug('Log thread joined')

        self._monitor_thread.join()
        logger.debug('Monitor thread joined')

        if self._pause_thread:
            self._pause_thread.join()
            logger.debug('Pause thread joined')

        self.status = 'Stopped'

    def _wait_for_pause(self):
        """ Wait for the _task_paused event to be set.

        """
        stop_sig = self._task_stop
        paused_sig = self._task_paused

        while not stop_sig.is_set():
            if paused_sig.wait(0.1):
                self.status = 'Paused'
                break

        resuming_sig = self._task_resume

        while not stop_sig.is_set():
            if resuming_sig.wait(1):
                self.status = 'Running'
                break
