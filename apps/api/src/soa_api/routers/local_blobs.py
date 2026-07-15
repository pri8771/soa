"""Local blob endpoint for the development filesystem store.

When ``storage_backend='filesystem'`` the object store issues signed URLs
that point here instead of at S3/MinIO. These routes let a browser PUT an
upload and GET a download exactly as it would against a presigned S3 URL —
the short-lived HMAC signature is the only authorization, so the routes are
intentionally public (no tenant auth). They exist only when the resolved
store is the FilesystemObjectStore; any other backend returns 404.

Never mounted with a production store — the filesystem backend is refused
in production by settings validation.
"""

from fastapi import APIRouter, Request, Response
from fastapi.responses import Response as RawResponse

from soa_storage.filesystem import FilesystemObjectStore
from soa_storage.store import ObjectNotFoundError, SignedUrlExpiredError

router = APIRouter(prefix="/_local-blobs", tags=["local-storage"])


def _store(request: Request) -> FilesystemObjectStore | None:
    deps = getattr(request.app.state, "dependencies", None)
    store = getattr(deps, "object_store", None)
    return store if isinstance(store, FilesystemObjectStore) else None


@router.put("/{key:path}")
async def put_blob(key: str, request: Request) -> Response:
    store = _store(request)
    if store is None:
        return Response(status_code=404)
    try:
        verified_key, method = store.verify_signed_url(str(request.url))
    except SignedUrlExpiredError:
        return Response(status_code=403, content=b"signed URL expired")
    except ValueError:
        return Response(status_code=403, content=b"invalid signature")
    if method != "PUT" or verified_key != key:
        return Response(status_code=403, content=b"signature/method mismatch")
    body = await request.body()
    content_type = request.headers.get("content-type", "application/octet-stream")
    await store.put(key, body, content_type=content_type)
    return Response(status_code=200)


@router.get("/{key:path}")
async def get_blob(key: str, request: Request) -> Response:
    store = _store(request)
    if store is None:
        return Response(status_code=404)
    try:
        verified_key, method = store.verify_signed_url(str(request.url))
    except SignedUrlExpiredError:
        return Response(status_code=403, content=b"signed URL expired")
    except ValueError:
        return Response(status_code=403, content=b"invalid signature")
    if method != "GET" or verified_key != key:
        return Response(status_code=403, content=b"signature/method mismatch")
    try:
        data = await store.get(key)
        meta = await store.head(key)
    except ObjectNotFoundError:
        return Response(status_code=404)
    return RawResponse(content=data, media_type=meta.content_type)
