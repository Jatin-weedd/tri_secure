"""
India TRI Data Gateway — Secure Resolution Proxy & Gatekeeper
─────────────────────────────────────────────────────────────────────────────
"""
import os
import time
import threading
from typing import Dict, Optional, Tuple

import requests
import rasterio
from rasterio.vrt import WarpedVRT
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
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

if not HF_TOKEN:
    raise RuntimeError("HF_TOKEN environment variable is missing on Render.")

app = FastAPI(
    title="India TRI Data Gateway",
    description="Authenticated secure router and resolver for private raster layers.",
    version="1.3.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("ALLOWED_ORIGINS", "*").split(","),
    allow_methods=["GET", "HEAD", "OPTIONS"],
    allow_headers=["*"],
)

def check_api_key(x_api_key: Optional[str]) -> str:
    if CLIENT_API_KEY and x_api_key != CLIENT_API_KEY:
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key header.")
    return x_api_key or "anonymous"

def _resolve_signed_url(repo_id: str, filename: str, repo_type: str = "dataset") -> str:
    """Resolves Hugging Face LFS redirect to return a direct pre-signed S3/CDN URL."""
    raw_url = hf_hub_url(repo_id=repo_id, filename=filename, repo_type=repo_type)
    resp = requests.head(
        raw_url,
        headers={"Authorization": f"Bearer {HF_TOKEN}"},
        allow_redirects=True,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.url

# ── Tile Footprint Indexing ──────────────────────────────────────────────────
_index_lock = threading.Lock()
_tile_bounds: Dict[str, Tuple[float, float, float, float]] = {}
_index_ready: bool = False

def _build_tile_index() -> None:
    global _index_ready
    with _index_lock:
        if _index_ready:
            return
        gdal_opts = {
            "GDAL_HTTP_HEADERS": f"Authorization: Bearer {HF_TOKEN}",
            "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
            "CPL_VSIL_CURL_USE_HEAD": "NO",
            "VSI_CACHE": "TRUE",
            "VSI_CACHE_SIZE": "16000000",
        }
        for fname in TRI_TILES:
            try:
                raw_url = hf_hub_url(repo_id=TRI_REPO, filename=fname, repo_type=REPO_TYPE)
                url = f"/vsicurl/{raw_url}"
                with rasterio.Env(**gdal_opts):
                    with rasterio.open(url) as src:
                        with WarpedVRT(src, crs=WEB_CRS, src_nodata=NODATA, nodata=NODATA) as vrt:
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

# ── Routes ───────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/api/v1/manifest")
def manifest(request: Request, x_api_key: Optional[str] = Header(None)):
    check_api_key(x_api_key)
    _ensure_index()
    return JSONResponse({
        "crs": "EPSG:4326",
        "nodata": NODATA,
        "tiles": [
            {
                "filename": fname,
                "bounds": {"west": l, "south": b, "east": r, "north": t},
            }
            for fname, (l, b, r, t) in _tile_bounds.items()
        ],
    })

@app.get("/api/v1/resolve/watershed")
def resolve_watershed(x_api_key: Optional[str] = Header(None)):
    check_api_key(x_api_key)
    url = _resolve_signed_url(WATERSHED_REPO, WATERSHED_FILE)
    return {"url": url}

@app.get("/api/v1/resolve/tile/{filename}")
def resolve_tile(filename: str, x_api_key: Optional[str] = Header(None)):
    check_api_key(x_api_key)
    if filename not in TRI_TILES:
        raise HTTPException(status_code=404, detail="Unknown tile filename.")
    url = _resolve_signed_url(TRI_REPO, filename)
    return {"url": url}
