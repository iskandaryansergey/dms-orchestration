"""Errors raised by Lambdas on purpose.

Step Functions reports a Lambda function error by the exception class name, so
the class names below are the error names used in the state machine's Catch
rules and in the run report.
"""


class OrchestratorError(Exception):
    """Base class; never caught by name in ASL."""


class InvalidRunRequest(OrchestratorError):  # noqa: N818 - name is the SFN error name
    pass


class UnknownTask(OrchestratorError):  # noqa: N818
    pass


class JobSetNotFound(OrchestratorError):  # noqa: N818
    pass


class InvalidJobSet(OrchestratorError):  # noqa: N818
    pass


class DmsTaskNotFound(OrchestratorError):  # noqa: N818
    pass


class JobFailed(OrchestratorError):  # noqa: N818
    pass


class TaskConfigInvalid(OrchestratorError):  # noqa: N818
    """The DMS task cannot run as configured (table mapping, task settings, endpoints)."""


class RunAlreadyActive(OrchestratorError):  # noqa: N818
    """Another run (or the same run's post) holds the task lock."""


class ContextNotPinned(OrchestratorError):  # noqa: N818
    """post found no context.json version recorded by migrate (RUN# contextVersionId), or not that version."""
