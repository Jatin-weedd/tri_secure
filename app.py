"""
India TRI Data Gateway — Secure Tokenless Resolution Proxy (v1.2.0)
───────────────────────────────────────────────────────────────────
"""
import os
import time
import uuid
import threading
from contextlib import contextmanager
from typing import Dict, List, Optional, Tuple

import httpx
import requests
import rasterio
from rasterio.vrt import WarpedVRT
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, JSONResponse, RedirectResponse
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

MAX_RANGE_BYTES          = 8 * 1024 * 1024          
DAILY_BYTE_QUOTA_PER_KEY  = 500 * 1024 * 1024        

# ── Protection Rules (Configured for ~25 Sites/Day) ─────────────────────────
BURST_LIMIT  = 12       # Max 12 resolve requests per minute (allows ~2-3 sites/min)
BURST_WINDOW = 60       # 60 seconds

DAILY_QUOTA  = 100      # Max 100 resolve requests per 24 hours (allows ~25-30 sites/day)
DAILY_WINDOW = 86400    # 24 hours (in seconds)

TICKET_TTL_SECONDS = 60 # Gateway ticket self-destructs after 60 seconds

if not HF_TOKEN:
    raise RuntimeError("HF_TOKEN environment variable is missing on Render.")

app = FastAPI(
    title="India TRI Data Gateway",
    description="Authenticated secure signing router for private raster and vector layers.",
    version="1.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("ALLOWED_ORIGINS", "*").split(","),
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Range", "Content-Length", "Accept-Ranges"],
)

# ── Security & Memory Vaults ────────────────────────────────────────────────
_resolve_history: Dict[str, List[float]] = {}
_rate_limit_lock = threading.Lock()

_ticket_vault: Dict[str, Tuple[str, float]] = {}
_ticket_lock = threading.Lock()

def check_api_key(x_api_key: Optional[str]) -> str:
    if CLIENT_API_KEY and x_api_key != CLIENT_API_KEY:
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key header.")
    return x_api_key or "anonymous"

def _enforce_resolve_limits(api_key: str) -> None:
    """Enforces a 5 req/min burst limit AND a 20 req/day quota per API key."""
    now = time.time()
    day_ago = now - DAILY_WINDOW
    minute_ago = now - BURST_WINDOW

    with _rate_limit_lock:
        history = _resolve_history.get(api_key, [])
        valid_24h_history = [ts for ts in history if ts > day_ago]

        # 1. Burst Check (Max 5 per minute)
        recent_1min = [ts for ts in valid_24h_history if ts > minute_ago]
        if len(recent_1min) >= BURST_LIMIT:
            retry_in = int((recent_1min[0] + BURST_WINDOW) - now)
            raise HTTPException(
                status_code=429,
                detail=f"Burst limit reached: Max {BURST_LIMIT} resolves per minute. Wait {max(1, retry_in)} seconds."
            )

        # 2. Daily Quota Check (Max 20 per 24h)
        if len(valid_24h_history) >= DAILY_QUOTA:
            retry_in = int((valid_24h_history[0] + DAILY_WINDOW) - now)
            hours_left = round(retry_in / 3600.0, 1)
            raise HTTPException(
                status_code=429,
                detail=f"Daily quota reached: Max {DAILY_QUOTA} signed URLs per 24 hours. Resets in ~{hours_left} hours."
            )

        valid_24h_history.append(now)
        _resolve_history[api_key] = valid_24h_history

def _create_ticket(real_cdn_url: str) -> str:
    """Creates a short-lived UUID ticket that self-destructs after 60 seconds."""
    ticket_id = str(uuid.uuid4())
    now = time.time()
    with _ticket_lock:
        # Purge stale expired tickets
        expired = [t for t, (_, exp) in _ticket_vault.items() if exp < now]
        for t in expired:
            del _ticket_vault[t]
        _ticket_vault[ticket_id] = (real_cdn_url, now + TICKET_TTL_SECONDS)
    return ticket_id

# ── GDAL Helpers ────────────────────────────────────────────────────────────
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

# SECURE RESOLUTION ENDPOINTS WITH TICKET WRAPPER
@app.get("/api/v1/resolve/watershed")
def resolve_watershed(request: Request, x_api_key: Optional[str] = Header(None)):
    api_key = check_api_key(x_api_key)
    _enforce_resolve_limits(api_key)
    
    real_cdn_url = _resolve_signed_url(WATERSHED_REPO, WATERSHED_FILE)
    ticket_id = _create_ticket(real_cdn_url)
    
    base = str(request.base_url).rstrip("/")
    return {"url": f"{base}/s/{ticket_id}"}

@app.get("/api/v1/resolve/tile/{filename}")
def resolve_tile(filename: str, request: Request, x_api_key: Optional[str] = Header(None)):
    api_key = check_api_key(x_api_key)
    _enforce_resolve_limits(api_key)
    
    if filename not in TRI_TILES:
        raise HTTPException(status_code=404, detail="Unknown tile filename.")
        
    real_cdn_url = _resolve_signed_url(TRI_REPO, filename)
    ticket_id = _create_ticket(real_cdn_url)
    
    base = str(request.base_url).rstrip("/")
    return {"url": f"{base}/s/{ticket_id}"}

# 60-SECOND TICKET REDIRECT ROUTE
@app.get("/s/{ticket_id}")
def stream_redirect(ticket_id: str):
    now = time.time()
    with _ticket_lock:
        record = _ticket_vault.get(ticket_id)
        if not record:
            raise HTTPException(status_code=410, detail="Ticket link has expired or is invalid.")
            
        cdn_url, expires_at = record
        if now > expires_at:
            del _ticket_vault[ticket_id]
            raise HTTPException(status_code=410, detail="Ticket link expired (60-second limit reached).")
            
    return RedirectResponse(url=cdn_url, status_code=307)

# MODE B RANGE PROXY ROUTE
@app.get("/tiles/{filename}")
async def get_tile_bytes(filename: str, request: Request, x_api_key: Optional[str] = Header(None)):
    check_api_key(x_api_key)
    if filename not in TRI_TILES: raise HTTPException(status_code=404)
    range_header = request.headers.get("range")
    if not range_header: raise HTTPException(status_code=416)
    span = _parse_explicit_range_span(range_header)
    if span is None or span > MAX_RANGE_BYTES: raise HTTPException(status_code=416)
    
    upstream_url = _tile_url(filename)
    upstream_headers = {"Authorization": f"Bearer {HF_TOKEN}", "Range": range_header}
    async with httpx.AsyncClient(timeout=30.0) as client:
        upstream_resp = await client.get(upstream_url, headers=upstream_headers, follow_redirects=True)
    if upstream_resp.status_code not in (200, 206): raise HTTPException(status_code=502)
    
    body = upstream_resp.content
    passthrough_headers = {"Accept-Ranges": "bytes"}
    for h in ("Content-Range", "Content-Length"):
        if h in upstream_resp.headers: passthrough_headers[h] = upstream_resp.headers[h]
    return Response(content=body, status_code=upstream_resp.status_code, headers=passthrough_headers)

def _parse_explicit_range_span(range_header: str) -> Optional[int]:
    try:
        unit, _, spec = range_header.partition("=")
        if unit.strip().lower() != "bytes" or "," in spec: return None
        start_str, _, end_str = spec.partition("-")
        return int(end_str) - int(start_str) + 1
    except Exception: return None
