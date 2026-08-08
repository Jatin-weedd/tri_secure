"""
India TRI Data Gateway — Secure Tokenless Resolution Proxy & Metered Gatekeeper
─────────────────────────────────────────────────────────────────────────────
"""
import os
import time
import threading
from contextlib import contextmanager
from typing import Dict, Optional, Tuple

import httpx
import requests
import rasterio
from rasterio.vrt import WarpedVRT
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse
from huggingface_hub import hf_hub_url

# ── Config ──────────────────────────────────────────────────────────────────
HF_TOKEN       = os.environ.get("HF_TOKEN")
CLIENT_API_KEY = os.environ.get("CLIENT_API_KEY")

TRI_REPO       = "J2003S/india-tri"
TRI_TILES      = [f"TRI_Tile{i}_COG.tif" for i in range(1, 10)]
REPO_TYPE      = "dataset"
WEB_CRS        = "EPSG:4326"
NODATA         = -9999.0

WATERSHED_REPO = "J2003S/hydrology-data-vault"
WATERSHED_FILE = "Watershed.fgb"

MAX_RANGE_BYTES          = 8 * 1024 * 1024          # Max 8 MB per chunk
DAILY_BYTE_QUOTA_PER_KEY = 500 * 1024 * 1024        # Max 500 MB daily limit

if not HF_TOKEN:
    raise RuntimeError("HF_TOKEN environment variable is missing on Render.")

# ── In-Memory Quota Ledger ───────────────────────────────────────────────────
_usage_ledger: Dict[str, dict] = {}
_ledger_lock: threading.Lock = threading.Lock()

app = FastAPI(
    title="India TRI Data Gateway",
    description="Authenticated secure router and metered proxy for private raster layers.",
    version="1.2.2",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("ALLOWED_ORIGINS", "*").split(","),
    allow_methods=["GET", "HEAD", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Range", "Content-Length", "Accept-Ranges"],
)

def _tile_url(filename: str) -> str:
    return hf_hub_url(repo_id=TRI_REPO, filename=filename, repo_type=REPO_TYPE)

def _auth_headers() -> str:
    return f"Authorization: Bearer {HF_TOKEN}"

GDAL_OPTS = {
    "GDAL_HTTP_HEADERS": _auth_headers(),
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_USE_HEAD": "NO",
    "VSI_CACHE": "TRUE",
    "VSI_CACHE_SIZE": "16000000",
    "GDAL_HTTP_MULTIRANGE": "YES",
}

@contextmanager
def open_tile_as_4326(filename: str):
    url = f"/vsicurl/{_tile_url(filename)}"
    with rasterio.Env(**GDAL_OPTS):
        with rasterio.open(url) as src:
            with WarpedVRT(src, crs=WEB_CRS, src_nodata=NODATA, nodata=NODATA) as vrt:
                yield vrt

# ── Tile Footprint Indexing ──────────────────────────────────────────────────
_index_lock: threading.Lock = threading.Lock()
_tile_bounds: Dict[str, Tuple[float, float, float, float]] = {}
_index_ready: bool = False

def _build_tile_index() -> None:
    global _index_ready
    with _index_lock:
        if _index_ready:
            return
        for fname in TRI_TILES:
            try:
                with open_tile_as_4326(fname) as vrt:
                    b = vrt.bounds
                    _tile_bounds[fname] = (b.left, b.bottom, b.right, b.top)
            except Exception as e:
                print(f"[startup] WARNING: failed to read bounds for {fname}: {e}")
        _index_ready = True

@app.on_event("startup")
def _on_startup():
    _build_tile_index()

def _ensure_index() -> None:
    if not _index_ready:
        _build_tile_index()

def check_api_key(x_api_key: Optional[str]) -> str:
    if CLIENT_API_KEY and x_api_key != CLIENT_API_KEY:
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key header.")
    return x_api_key or "anonymous"

def _check_and_record_quota(api_key: str, requested_bytes: int):
    now = time.time()
    with _ledger_lock:
        record = _usage_ledger.get(api_key)
        if not record or now > record["reset_at"]:
            record = {"bytes": 0, "reset_at": now + 86400}
            _usage_ledger[api_key] = record

        if record["bytes"] + requested_bytes > DAILY_BYTE_QUOTA_PER_KEY:
            raise HTTPException(
                status_code=429,
                detail="Daily data transfer limit (500 MB) exceeded for this API key."
            )

        record["bytes"] += requested_bytes

def _parse_and_clamp_range(range_header: str, max_bytes: int) -> Tuple[Optional[str], Optional[int]]:
    try:
        unit, _, spec = range_header.partition("=")
        if unit.strip().lower() != "bytes" or "," in spec:
            return None, None
        
        start_str, _, end_str = spec.partition("-")
        start = int(start_str.strip())
        
        if end_str.strip():
            end = int(end_str.strip())
            span = end - start + 1
            if span > max_bytes:
                end = start + max_bytes - 1
                span = max_bytes
        else:
            end = start + max_bytes - 1
            span = max_bytes
            
        return f"bytes={start}-{end}", span
    except Exception:
        return None, None

async def _resolve_s3_cdn_url(filename: str) -> str:
    """Resolves HF 302 redirect to obtain the raw pre-signed S3 CDN URL."""
    raw_url = _tile_url(filename)
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.head(
            raw_url,
            headers={"Authorization": f"Bearer {HF_TOKEN}"},
            follow_redirects=False
        )
        if resp.status_code in (301, 302, 303, 307) and "location" in resp.headers:
            return resp.headers["location"]
        return raw_url

def _resolve_signed_url(repo_id: str, filename: str, repo_type: str = "dataset") -> str:
    raw_url = hf_hub_url(repo_id=repo_id, filename=filename, repo_type=repo_type)
    resp = requests.head(
        raw_url,
        headers={"Authorization": f"Bearer {HF_TOKEN}"},
        allow_redirects=True,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.url

# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/api/v1/manifest")
def manifest(request: Request, x_api_key: Optional[str] = Header(None)):
    check_api_key(x_api_key)
    _ensure_index()
    base = str(request.base_url).rstrip("/")
    return JSONResponse({
        "crs": "EPSG:4326",
        "nodata": NODATA,
        "tiles": [
            {
                "filename": fname,
                "bounds": {"west": l, "south": b, "east": r, "north": t},
                "url": f"{base}/tiles/{fname}",
            }
            for fname, (l, b, r, t) in _tile_bounds.items()
        ],
    })

@app.get("/api/v1/resolve/watershed")
def resolve_watershed(x_api_key: Optional[str] = Header(None)):
    check_api_key(x_api_key)
    url = _resolve_signed_url(WATERSHED_REPO, WATERSHED_FILE)
    return {"url": url}

# SECURE METERED TILE PROXY
@app.api_route("/tiles/{filename}", methods=["GET", "HEAD"])
async def get_tile_bytes(filename: str, request: Request, x_api_key: Optional[str] = Header(None)):
    api_key = check_api_key(x_api_key)
    
    if filename not in TRI_TILES:
        raise HTTPException(status_code=404, detail="Unknown tile filename.")

    # 1. Obtain pre-signed S3 CDN URL
    s3_cdn_url = await _resolve_s3_cdn_url(filename)

    # 2. Handle HEAD requests (do NOT pass Authorization header to S3)
    if request.method == "HEAD":
        async with httpx.AsyncClient(timeout=30.0) as client:
            upstream_resp = await client.head(s3_cdn_url)
        headers = {"Accept-Ranges": "bytes"}
        for h in ("Content-Length", "Content-Type"):
            if h in upstream_resp.headers:
                headers[h] = upstream_resp.headers[h]
        return Response(status_code=upstream_resp.status_code, headers=headers)

    # 3. Handle GET range requests
    range_header = request.headers.get("range")
    if not range_header:
        range_header = f"bytes=0-{MAX_RANGE_BYTES - 1}"

    clamped_range, span = _parse_and_clamp_range(range_header, MAX_RANGE_BYTES)
    if not clamped_range or span is None:
        raise HTTPException(status_code=400, detail="Invalid Range header format.")
    
    _check_and_record_quota(api_key, span)
    
    # 4. Proxy range request directly to S3 without Authorization header
    async with httpx.AsyncClient(timeout=30.0) as client:
        upstream_resp = await client.get(s3_cdn_url, headers={"Range": clamped_range})
        
    if upstream_resp.status_code not in (200, 206):
        raise HTTPException(status_code=502, detail="Error fetching byte range from upstream storage.")
    
    body = upstream_resp.content
    passthrough_headers = {"Accept-Ranges": "bytes"}
    for h in ("Content-Range", "Content-Length"):
        if h in upstream_resp.headers:
            passthrough_headers[h] = upstream_resp.headers[h]
            
    return Response(content=body, status_code=upstream_resp.status_code, headers=passthrough_headers)
