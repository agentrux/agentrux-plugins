"""SDK-specific errors."""


class SDKError(Exception):
    """Base SDK error."""


class SequenceUnavailableError(SDKError):
    """sequence_no is missing from server response."""


class TokenExpiredError(SDKError):
    """JWT token has expired and refresh failed."""


class GapBackfillDepthExceeded(SDKError):
    """Maximum backfill recursion depth exceeded."""


class GapUnrecoverable(SDKError):
    """A gap in the event stream cannot be filled (retention boundary or
    physically deleted by cleanup job). The application is notified via
    on_unrecoverable callback; this exception is not normally raised.
    """


class CheckpointLockedError(SDKError):
    """Another process holds the checkpoint file lock."""


class CheckpointOrderError(SDKError):
    """A save() call attempted to record a sequence_no smaller than the
    last successfully saved one. Surfaces strict-in-order violations.
    """


class PayloadTooLargeError(SDKError):
    """Message payload exceeds size limit."""


class ClientSecretExpiredError(SDKError):
    """Script Client secret has expired and must be rotated by an administrator."""
