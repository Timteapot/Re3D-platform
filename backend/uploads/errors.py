class UploadError(Exception):
    """Base class for upload workflow failures safe to translate at the API."""


class UploadNotFoundError(UploadError):
    """The upload does not exist for the current user."""


class UploadConflictError(UploadError):
    """The upload state or image identity conflicts with the request."""


class UploadValidationError(UploadError):
    """Uploaded content does not satisfy the controlled input rules."""


class UploadTooLargeError(UploadValidationError):
    """An individual image or the complete upload exceeds its byte limit."""
