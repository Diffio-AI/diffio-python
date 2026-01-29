class DiffioApiError(Exception):
    def __init__(self, message, statusCode=None, responseBody=None):
        super().__init__(message)
        self.message = message
        self.statusCode = statusCode
        self.responseBody = responseBody

    def __str__(self):
        details = []
        if self.statusCode is not None:
            details.append(f"statusCode={self.statusCode}")
        if self.responseBody is not None:
            details.append("responseBody set")
        suffix = f" ({', '.join(details)})" if details else ""
        return f"{self.message}{suffix}"
