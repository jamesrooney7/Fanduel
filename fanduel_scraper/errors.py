"""Exception hierarchy. Every error carries an actionable hint the CLI prints."""


class ScraperError(Exception):
    exit_code = 1
    hint = ""

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        if hint is not None:
            self.hint = hint


class InvalidEventRefError(ScraperError):
    exit_code = 2


class GeoBlockedError(ScraperError):
    exit_code = 3


class EventNotFoundError(ScraperError):
    exit_code = 4


class SchemaDriftError(ScraperError):
    exit_code = 5


class SheetsError(ScraperError):
    exit_code = 6


class SheetsAuthError(SheetsError):
    pass


class SheetsAccessError(SheetsError):
    pass


class SheetsQuotaError(SheetsError):
    pass


class NetworkError(ScraperError):
    exit_code = 7
