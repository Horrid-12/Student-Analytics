from mangum import Mangum
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.main import app as fastapi_app

# Debug aid while the Vercel prefix is being nailed down: any Starlette 404 gets
# the path the function actually received, so a still-misrouted deploy reports
# it in the response body instead of a bare {"detail": "Not Found"}.
async def _not_found(request: Request, exc: StarletteHTTPException):
    path = request.scope.get("path", "")
    accept = request.headers.get("accept", "")
    if path.startswith(("/api", "/analysis/", "/upload", "/roster/")) or path.endswith("/export") or "application/json" in accept:
        return JSONResponse(
            status_code=404,
            content={"detail": "Not Found", "received_path": path},
        )
    from app.main import _not_found_response
    return _not_found_response(request)


fastapi_app.add_exception_handler(StarletteHTTPException, _not_found)

# Vercel serves all routes through this function via a rewrite of /(.*) ->
# /api/index. The rewrite changes the path the function receives to the
# function's deployment URL — which Vercel reports variously as "/api/index.py",
# "/api/index" or "/api/..." — so the FastAPI router would 404 every real
# route. Restore the original route segment: strip the longest known prefix and
# guarantee the remainder starts with "/". The app has no genuine /api routes
# to collide with.
_PREFIXES = ("/api/index.py", "/api/index", "/api")


class _StripVercelPrefix:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            for prefix in _PREFIXES:
                if path == prefix:
                    scope["path"] = "/"
                    break
                if path.startswith(prefix + "/"):
                    remainder = path[len(prefix):]
                    scope["path"] = remainder if remainder.startswith("/") else "/" + remainder
                    break
        await self.app(scope, receive, send)


wrapped = _StripVercelPrefix(fastapi_app)
app = Mangum(wrapped)
handler = app