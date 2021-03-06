# Copyright 2014 Google Inc. All Rights Reserved.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""The main OpenHTF entry point."""

import argparse
import collections
import copy
import inspect
import itertools
import json
import logging
import signal
import socket
import sys
import threading

import mutablerecords

from enum import Enum

from openhtf import conf
from openhtf import exe
from openhtf import plugs
from openhtf import util
from openhtf.exe import phase_executor
from openhtf.exe import triggers
from openhtf.io import http_api
from openhtf.io import test_record
from openhtf.io import user_input
from openhtf.util import functions
from openhtf.util import logs
from openhtf.util import measurements

# All tests require a station_id.  This can be via the --config-file
# automatically loaded by OpenHTF, provided explicitly to the config with
# conf.Load(station_id='My_OpenHTF_Station'), or alongside other configs loaded
# with conf.LoadFromDict({..., 'station_id': 'My_Station'}).  If none of those
# are provided then we'll fall back to the machine's hostname.
conf.Declare('station_id', 'The name of this test station',
             default_value=socket.gethostname())

__version__ = util.get_version()
_LOG = logging.getLogger(__name__)


class InvalidTestPhaseError(Exception):
  """Raised when an invalid method is decorated."""


class Test(object):
  """An object that represents an OpenHTF test.

  Example:

    def PhaseOne(test):
      # Integrate more widgets

    def PhaseTwo(test):
      # Analyze widget integration status

    Test(PhaseOne, PhaseTwo).Execute()

  Note that Test() objects *must* be created in the main thread, but can be
  .Execute()'d in a separate thread.
  """

  def __init__(self, *phases, **metadata):
    code_info = test_record.CodeInfo.ForModuleFromStack(levels_up=2)
    self._test_options = TestOptions()
    self._test_data = TestData(phases, metadata=metadata, code_info=code_info)
    self._test_data.metadata['config'] = conf._asdict()
    self._lock = threading.Lock()
    self._executor = None
    # Make sure Configure() gets called at least once before Execute().  The
    # user might call Configure() again to override options, but we don't want
    # to force them to if they want to use defaults.  For default values, see
    # the class definition of TestOptions.
    self.Configure()
    # TODO(madsci): Fix this to play nice with multiple Test instances.
    signal.signal(signal.SIGINT, self.StopFromSigInt)

  def AddOutputCallback(self, callback):
    """DEPRECATED: Use AddOutputCallbacks() instead."""
    # TODO(madsci): Remove this before we push to PyPI, here for transitionary
    # purposes.
    raise AttributeError(
        'DEPRECATED, use AddOutputCallbacks() instead of AddOutputCallback()')

  def AddOutputCallbacks(self, *callbacks):
    """Add the given function as an output module to this test."""
    self._test_options.output_callbacks.extend(callbacks)

  def OutputTestRecord(self, record):
    """Feed the record of this test to all output modules."""
    for output_cb in self._test_options.output_callbacks:
      try:
        output_cb(record)
      except Exception:
        _LOG.exception(
            'Output callback %s errored out; continuing anyway', output_cb)

  @property
  def data(self):
    return self._test_data

  def Configure(self, **kwargs):
    """Update test-wide configuration options.

    Valid kwargs:
      http_port: Port on which to run the http_api, or None to disable.
      output_callbacks: List of output callbacks to run, typically it's better
          to use AddOutputCallbacks(), but you can pass [] here to reset them.
      teardown_function: Function to run at teardown.  We pass the same
          arguments to it as a phase.
    """
    # These internally ensure they are safe to call multiple times with no weird
    # side effects.
    CreateArgParser(add_help=True).parse_known_args()
    logs.SetupLogger()
    for key, value in kwargs.iteritems():
      setattr(self._test_options, key, value)

  def StopFromSigInt(self, *_):
    """Stop test execution as abruptly as we can, only in response to SIGINT."""
    _LOG.error('Received SIGINT.')
    with self._lock:
      _LOG.error('Stopping Test due to SIGINT')
      if self._executor:
        # TestState str()'s nicely to a descriptive string, so let's log that
        # just for good measure.
        _LOG.error('Test state: %s', self._executor.GetState())
        self._executor.Stop()
        self._executor = None
    # The default SIGINT handler does this. If we don't, then nobody above
    # us is notified of the event. This will raise this exception in the main
    # thread.
    raise KeyboardInterrupt()

  def Execute(self, test_start=None, loop=None):
    """Starts the framework and executes the given test.

    Args:
      test_start: Trigger for starting the test, defaults to not setting the DUT
          serial number.
      loop: DEPRECATED
    """
    # TODO(madsci): Remove this after a transitionary period.
    if loop is not None:
      raise ValueError(
          'DEPRECATED. Looping is no longer natively supported by OpenHTF, '
          'use a while True: loop around Test.Execute() instead.')

    # We have to lock this section to ensure we don't call
    # TestExecutor.StopFromSigInt() in self.Stop() between instantiating it and
    # .Start()'ing it.
    with self._lock:
      self._executor = exe.TestExecutor(self._test_data, plugs.PlugManager(),
                                        self._test_options.teardown_function)
      _LOG.info('Executing test: %s', self.data.code_info.name)
      self._executor.SetTestStart(test_start)
      http_server = None
      if self._test_options.http_port:
        http_server = http_api.Server(
            self._executor, self._test_options.http_port)
        http_server.Start()

      self._executor.Start()

    try:
      self._executor.Wait()
    finally:
      # If the framework doesn't transition from INITIALIZING to EXECUTING
      # then test state isn't set and there's no record to output.
      if self._executor and self._executor.GetState():
        record = self._executor.GetState().GetFinishedRecord()
        self.OutputTestRecord(record)
      if http_server:
        http_server.Stop()
      self._executor = None


class TestOptions(mutablerecords.Record('TestOptions', [], {
    'http_port': http_api.DEFAULT_HTTP_PORT,
    'output_callbacks': list,
    'teardown_function': None,
})):
  """Class encapsulating various tunable knobs for Tests and their defaults."""


class TestData(collections.namedtuple(
    'TestData', ['phases', 'code_info', 'metadata'])):
  """An object that represents the reusable portions of an OpenHTF test.

  This object encapsulates the static test information that is set once and used
  by the framework along the way.

  Attributes:
    phases: The phases to execute for this test.
    metadata: Any metadata that should be associated with test records.
    code_info: Information about the module that created the test.
  """

  def __new__(cls, phases, code_info, metadata):
    phases = [PhaseInfo.WrapOrCopy(phase) for phase in phases]
    return super(TestData, cls).__new__(cls, phases, code_info, metadata)

  @property
  def plug_types(self):
    """Returns set of plug types required by this test."""
    return {plug.cls for phase in self.phases for plug in phase.plugs}


def CreateArgParser(add_help=False):
  """Creates an argparse.ArgumentParser for parsing command line flags.

  If you want to add arguments, create your own with this as a parent:

  >>> parser = argparse.ArgumentParser(
          'My args title', parents=[openhtf.CreateArgParser()])
  >>> parser.parse_args()
  """
  return argparse.ArgumentParser('OpenHTF-based testing', parents=[
      conf.ARG_PARSER, user_input.ARG_PARSER, phase_executor.ARG_PARSER,
      logs.ARG_PARSER], add_help=add_help)


# Result of a phase.
#
# These values can be returned by a test phase to control what the framework
# does after the phase.  CONTINUE causes the framework to execute the next
# phase, REPEAT causes the framework to execute that same phase again, and STOP
# causes the framework to stop executing.
PhaseResult = Enum('PhaseResult', ['CONTINUE', 'REPEAT', 'STOP'])


class PhaseOptions(mutablerecords.Record(
    'PhaseOptions', [], {'timeout_s': None, 'run_if': None})):
  """Options used to override default test phase behaviors.

  Attributes:
    timeout_s: Timeout to use for the phase, in seconds.
    run_if: Callback that decides whether to run the phase or not.  The
      callback will be passed the phase_data the phase would be run with.

  Example Usage:
    @PhaseOptions(timeout_s=1)
    def PhaseFunc(test):
      pass
  """

  def __call__(self, phase_func):
    phase = PhaseInfo.WrapOrCopy(phase_func)
    for attr in self.__slots__:
      value = getattr(self, attr)
      if value is not None:
        setattr(phase.options, attr, value)
    return phase


class PhasePlug(mutablerecords.Record(
    'PhasePlug', ['name', 'cls'], {'update_kwargs': True})):
  """Information about the use of a plug in a phase."""


class PhaseInfo(mutablerecords.Record(
    'PhaseInfo', ['func', 'code_info'],
    {'options': PhaseOptions, 'plugs': list, 'measurements': list,
     'extra_kwargs': dict})):
  """Phase function and related information.

  Attributes:
    func: Function to be called (with phase_data as first argument).
    code_info: Info about the source code of func.
    options: PhaseOptions instance.
    plugs: List of PhasePlug instances.
    measurements: List of Measurement objects.
  """

  @classmethod
  def WrapOrCopy(cls, func):
    """Return a new PhaseInfo from the given function or instance.

    We want to return a new copy so that you can reuse a phase with different
    options, plugs, measurements, etc.

    Args:
      func: A phase function or PhaseInfo instance.

    Returns:
      A new PhaseInfo object.
    """
    if not isinstance(func, cls):
      func = cls(func, test_record.CodeInfo.ForFunction(func))
    # We want to copy so that a phase can be reused with different options, etc.
    return mutablerecords.CopyRecord(func)

  @property
  def name(self):
    return self.func.__name__

  @property
  def doc(self):
    return self.func.__doc__

  def WithArgs(self, **kwargs):
    """Send these keyword-arguments to the phase when called."""
    # Make a copy so we can have multiple of the same phase with different args
    # in the same test.
    new_info = mutablerecords.CopyRecord(self)
    new_info.extra_kwargs.update(kwargs)
    new_info.measurements = [m.WithArgs(**kwargs) for m in self.measurements]
    return new_info

  def __call__(self, phase_data):
    kwargs = dict(self.extra_kwargs)
    kwargs.update(phase_data.plug_manager.ProvidePlugs(
        (plug.name, plug.cls) for plug in self.plugs if plug.update_kwargs))
    arg_info = inspect.getargspec(self.func)
    # Pass in phase_data if it takes *args, or **kwargs with at least 1
    # positional, or more positional args than we have keyword args.
    if arg_info.varargs or (arg_info.keywords and len(arg_info.args) >= 1) or (
        len(arg_info.args) > len(kwargs)):
      # Underlying function has room for phase_data as an arg. If it doesn't
      # expect it but we miscounted args, we'll get another error farther down.
      return self.func(phase_data, **kwargs)
    return self.func(**kwargs)
