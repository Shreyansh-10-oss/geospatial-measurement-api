"""Framework-independent upload failures; HTTP mapping lives in the API layer."""


class UploadError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)
