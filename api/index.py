from mangum import Mangum

from app.main import app as fastapi_app

# Vercel serves all routes through this function via a rewrite of /(.*) ->
# /api/index. The rewrite changes the path the function receives to escaped
# "/api/index", so the FastAPI router would 404 on every real route. Strip the
# prefix back off so '/' / '/students' etc. route as intended; the app has no
# genuine /api routes to collide with.
_PREFIXES = ("/api/index", "/api")


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
                    scope["path"] = path[len(prefix):]
                    break
        await self.app(scope, receive, send)


wrapped = _StripVercelPrefix(fastapi_app)
handler = Mangum(wrapped)