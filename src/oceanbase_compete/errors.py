"""Errors with stable machine-readable outcomes."""


class ToolError(Exception):
    outcome = "error"
    exit_code = 1

    def __init__(self, message, **details):
        super().__init__(message)
        self.details = details


class AuthError(ToolError):
    outcome = "authentication_required"


class ProtocolError(ToolError):
    outcome = "protocol_error"


class OutcomeUnknown(ToolError):
    outcome = "submission_unconfirmed"
    exit_code = 4


class EvaluationFailed(ToolError):
    outcome = "evaluation_failed"
    exit_code = 2


class Pending(ToolError):
    outcome = "evaluation_pending"
    exit_code = 3


class DeadlineReached(Pending):
    outcome = "deadline_reached"
