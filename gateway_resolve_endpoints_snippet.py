"""
gateway_resolve_endpoints_snippet.py
─────────────────────────────────────
Merge these two routes into your existing gateway app (the one deployed on
Render, connected to your GitHub repo). This assumes FastAPI, matching the
X-API-Key convention your /api/v1/manifest and /tiles/{filename} routes
already use — adapt names (check_api_key, your manifest data source, etc.)
to fit your actual file structure.

WHY THIS EXISTS
Your /tiles/{filename} route re-implements HTTP byte-range serving itself,
and something about that implementation isn't fully compatible with GDAL's
/vsicurl/ driver (it fails with "not recognized as being in a supported
file format" when rasterio tries to open it). Rather than debug a hand-rolled
range server blind, these routes do the one thing that's actually required —
resolve a short-lived signed URL from Hugging Face using YOUR server-side
HF_TOKEN — and hand that URL back as JSON. The client then reads that signed
URL directly via /vsicurl/, which is the exact mechanism already proven to
work reliably (it's what direct-to-HF access in Colab was already doing).

Net effect: HF_TOKEN never leaves this server. The client only ever holds
CLIENT_API_KEY. Actual data bytes stream straight from HF's CDN (fast,
proven range-request support) — this server only ever handles small JSON
resolve calls, never the heavy raster/vector traffic.

SECURITY — IMPORTANT
Do NOT accept an arbitrary repo_id/filename from the client. Only resolve
files on a fixed allowlist. Otherwise CLIENT_API_KEY becomes a master key
to anything HF_TOKEN can read, not just this product's data.
"""

import os
import requests
from fastapi import Header, HTTPException
from huggingface_hub import hf_hub_url

# Keep server-side only — set as a Render environment variable/secret,
# never committed to the repo.
HF_TOKEN = os.environ["HF_TOKEN"]

WATERSHED_REPO = "J2003S/hydrology-data-vault"
WATERSHED_FILE = "Watershed.fgb"

TRI_DATA_REPO = "J2003S/india-tri"
# Populate this from whatever already backs your /api/v1/manifest response —
# reuse that data source rather than duplicating the tile list here.
KNOWN_TILE_FILENAMES = {
    "TRI_Tile1_COG.tif", "TRI_Tile2_COG.tif", "TRI_Tile3_COG.tif",
    "TRI_Tile4_COG.tif", "TRI_Tile5_COG.tif", "TRI_Tile6_COG.tif",
    "TRI_Tile7_COG.tif", "TRI_Tile8_COG.tif", "TRI_Tile9_COG.tif",
}


def _resolve_signed_url(repo_id: str, filename: str, repo_type: str = "dataset") -> str:
    """Server-side only: uses HF_TOKEN to get a short-lived signed URL."""
    raw_url = hf_hub_url(repo_id=repo_id, filename=filename, repo_type=repo_type)
    resp = requests.head(
        raw_url,
        headers={"Authorization": f"Bearer {HF_TOKEN}"},
        allow_redirects=True,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.url  # the resolved, signed, time-limited CDN URL


# Replace `check_api_key` with whatever your existing routes already use —
# this should be the same check /tiles/{filename} performs today.
def check_api_key(x_api_key: str):
    expected = os.environ.get("CLIENT_API_KEY")
    if expected and x_api_key != expected:
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key header.")


# ── Add these two routes to your existing `app = FastAPI(...)` instance ────

@app.get("/api/v1/resolve/watershed")
def resolve_watershed(x_api_key: str = Header(...)):
    check_api_key(x_api_key)
    url = _resolve_signed_url(WATERSHED_REPO, WATERSHED_FILE)
    return {"url": url}


@app.get("/api/v1/resolve/tile/{filename}")
def resolve_tile(filename: str, x_api_key: str = Header(...)):
    check_api_key(x_api_key)
    if filename not in KNOWN_TILE_FILENAMES:
        raise HTTPException(status_code=404, detail="Unknown tile filename.")
    url = _resolve_signed_url(TRI_DATA_REPO, filename)
    return {"url": url}
