"""Mapping protocol errors onto HTTP responses.

One error taxonomy (:class:`~atlas_shared.protocol.errors.ErrorCode`) serves both
REST and WebSocket, so a failure means the same thing regardless of how it was
reached.
"""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from atlas_shared.protocol.errors import AtlasProtocolError, ErrorCode

__all__ = ["HTTP_STATUS_FOR_CODE", "install_exception_handlers"]

HTTP_STATUS_FOR_CODE: dict[ErrorCode, int] = {
    ErrorCode.MALFORMED: status.HTTP_400_BAD_REQUEST,
    ErrorCode.UNSUPPORTED_VERSION: status.HTTP_400_BAD_REQUEST,
    ErrorCode.UNSUPPORTED_TYPE: status.HTTP_400_BAD_REQUEST,
    ErrorCode.INVALID_KIND: status.HTTP_400_BAD_REQUEST,
    ErrorCode.UNAUTHORIZED: status.HTTP_401_UNAUTHORIZED,
    ErrorCode.SIGNATURE_INVALID: status.HTTP_401_UNAUTHORIZED,
    ErrorCode.FORBIDDEN: status.HTTP_403_FORBIDDEN,
    ErrorCode.SAFE_MODE: status.HTTP_403_FORBIDDEN,
    ErrorCode.REPLAY_DETECTED: status.HTTP_409_CONFLICT,
    ErrorCode.TOOL_NOT_IMPLEMENTED: status.HTTP_501_NOT_IMPLEMENTED,
    ErrorCode.RATE_LIMITED: status.HTTP_429_TOO_MANY_REQUESTS,
    ErrorCode.TIMEOUT: status.HTTP_504_GATEWAY_TIMEOUT,
    ErrorCode.INTERNAL: status.HTTP_500_INTERNAL_SERVER_ERROR,
}

#: Codes whose ``details`` may be echoed to the caller. Authentication failures
#: are excluded on purpose: telling a caller *why* a credential was rejected
#: helps them find a working one.
_DETAILS_SAFE_CODES = frozenset(
    {
        ErrorCode.MALFORMED,
        ErrorCode.UNSUPPORTED_VERSION,
        ErrorCode.UNSUPPORTED_TYPE,
        ErrorCode.INVALID_KIND,
        ErrorCode.TOOL_NOT_IMPLEMENTED,
    }
)


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AtlasProtocolError)
    async def _handle_protocol_error(_request: Request, exc: AtlasProtocolError) -> JSONResponse:
        body: dict[str, object] = {"code": exc.code.value, "message": exc.message}
        if exc.details and exc.code in _DETAILS_SAFE_CODES:
            body["details"] = exc.details

        headers = {}
        if exc.code in (ErrorCode.UNAUTHORIZED, ErrorCode.SIGNATURE_INVALID):
            headers["WWW-Authenticate"] = "Bearer"

        return JSONResponse(
            status_code=HTTP_STATUS_FOR_CODE.get(exc.code, status.HTTP_500_INTERNAL_SERVER_ERROR),
            content=body,
            headers=headers,
        )
