"""
Example client — proves the gateway works using nothing but plain rasterio.

This is the key point of the "dumb gatekeeper" design: because the gateway
speaks ordinary HTTP Range semantics, GDAL's own /vsicurl/ driver can point
straight at it, exactly as if it were any other remote COG. The only
difference from talking to Hugging Face directly is the header we send:
instead of an HF bearer token, we send our own X-API-Key.

Usage:
    pip install rasterio
    python read_via_gateway.py

Edit GATEWAY_URL and API_KEY below first.
"""

import rasterio
from rasterio.windows import from_bounds

GATEWAY_URL = "https://<your-service-name>.onrender.com"
API_KEY     = "dev-key"          # must match CLIENT_API_KEY on the server
TILE_FILE   = "TRI_Tile1_COG.tif"

# A small lat/lon box to read — replace with an area you know is covered by
# whichever tile you point at. Get real tile bounds first from:
#   curl -H "X-API-Key: $API_KEY" https://<your-service-name>.onrender.com/api/v1/manifest
BBOX = (77.0, 28.4, 77.05, 28.45)   # (minlon, minlat, maxlon, maxlat)


def main():
    url = f"/vsicurl/{GATEWAY_URL}/tiles/{TILE_FILE}"
    gdal_opts = {
        "GDAL_HTTP_HEADERS": f"X-API-Key: {API_KEY}",
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "CPL_VSIL_CURL_USE_HEAD": "NO",
        "VSI_CACHE": "TRUE",
        "GDAL_HTTP_MULTIRANGE": "YES",
    }

    with rasterio.Env(**gdal_opts):
        with rasterio.open(url) as src:
            print(f"Opened via gateway. CRS={src.crs}, size={src.width}x{src.height}, bounds={src.bounds}")

            window = from_bounds(*BBOX, transform=src.transform)
            arr = src.read(1, window=window)
            print(f"Read a {arr.shape} window. min={arr.min()}, max={arr.max()}")


if __name__ == "__main__":
    main()
