from __future__ import annotations

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from mcp_email_server.attachments.store import attachment_store


async def _serve_attachment(request: Request) -> Response:
    token = request.path_params["token"]
    filename = request.path_params["filename"]

    entry = attachment_store.get(token)
    if entry is None:
        return Response("Not found or expired", status_code=404)

    # Require the filename in the URL to match the stored filename so the
    # token alone is not enough to enumerate files at different paths.
    if entry.filename != filename:
        return Response("Not found or expired", status_code=404)

    return Response(
        content=entry.data,
        media_type=entry.mime_type,
        headers={"Content-Disposition": f'attachment; filename="{entry.filename}"'},
    )


attachment_route = Route("/attachments/{token}/{filename}", _serve_attachment, methods=["GET"])
