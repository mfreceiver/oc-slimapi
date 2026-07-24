from __future__ import annotations

from fastapi import APIRouter, Request

from ..gzip_util import json_response
from ..directory import validate_directory

router = APIRouter(prefix="/slimapi", tags=["sessions"])


@router.get("/sessions/{sid}/children")
async def children(request: Request, sid: str, directory: str | None = None):
    if directory is not None:
        directory = validate_directory(directory)
    value, version = await request.app.state.children.get_or_fetch(sid, directory)
    return json_response(
        value,
        headers={"X-Children-Version": str(version)},
        accept_encoding=request.headers.get("accept-encoding"),
    )
