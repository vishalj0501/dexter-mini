"""Tool-layer exceptions."""


class ToolError(Exception):
    """Recoverable tool failure."""


class NotFoundError(ToolError):
    pass


class InvalidStateError(ToolError):
    pass


class SchemaError(ToolError):
    pass
