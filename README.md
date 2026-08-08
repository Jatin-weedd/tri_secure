# 🏔️ India TRI Data Gateway (Render)

This is **not a rendering API**. It's a thin, authenticated, metered
gatekeeper in front of the raw private TRI COG tiles. Clients get the raw
raster bytes they ask for (via ordinary HTTP Range requests) and render or
analyze the data themselves, using their own GIS tooling. This replaces the
earlier server-side-rendering design.

**Data stays on Hugging Face.** The 9 TRI tiles remain in the private
dataset repo `J2003S/india-tri`. This gateway never downloads a whole tile
to disk or holds one fully in memory — it forwards exactly the byte range a
client asked for, nothing more.

## Why this design

```
Client (rasterio / GDAL / QGIS / geotiff.js)
        │
        │  GET /tiles/TRI_Tile3_COG.tif
        │  Range: bytes=1048576-1056767
        │  X-API-Key: <their key>
        ▼
This Gateway (Render)
        │  checks API key, checks/records usage quota,
        │  caps the range size, then forwards the SAME
        │  Range request upstream with the real token
        ▼
        │  Authorization: Bearer <HF_TOKEN>
        │  Range: bytes=1048576-1056767
        ▼
Private HF dataset: J2003S/india-tri/TRI_Tile3_COG.tif
```

The client never sees the Hugging Face URL or `HF_TOKEN` — only this
gateway's URL and their own `X-API-Key`.

Because there's no GDAL decoding, no numpy math, no image rendering at
request time — just checking a key, checking a counter, and forwarding an
HTTP call — this comfortably runs on Render's smallest free instance.

## What stops someone from downloading the whole dataset

Two real, enforced controls, not just a "trust the client" hope:

1. **`MAX_RANGE_BYTES`** (default 8 MB) — every single request must specify
   an explicit, closed `Range: bytes=start-end` header, and its span is
   checked *before* fetching upstream. Open-ended or multi-range requests
   are rejected outright.
2. **`DAILY_BYTE_QUOTA_PER_KEY`** (default 500 MB / rolling 24h) — bytes
   actually served are tracked per `X-API-Key` in memory. Once a key's quota
   is used up, further requests get `429` until the window rolls over.

**Known limitation:** usage tracking is in-memory, so it resets whenever the
process restarts — including Render's free-tier spin-down/spin-up cycle.
Fine for a first version. If you need quotas to survive restarts or to hold
across multiple instances later, swap the `_usage` dict for a Redis
`INCR`-with-`TTL` pattern — only `_check_and_record_usage()` would need to
change.

## 🔐 Environment Variables Setup (Required)

Set these directly as Environment Variables on the Render Web Service
(Render encrypts them at rest — no special reference syntax needed):

| Variable | Required | Purpose |
|---|---|---|
| `HF_TOKEN` | Yes | Read access to the private dataset repo `J2003S/india-tri`. |
| `CLIENT_API_KEY` | Recommended | Clients must send `X-API-Key: <value>` on every request. If unset, the gateway is open to anyone. |

## 🚀 Deploying

1. Push `Dockerfile`, `app.py`, `requirements.txt`, `.dockerignore` (root of the repo, not the `client_example/` folder) to a GitHub repo.
2. Sign up at [render.com](https://render.com) — no card required for the free tier.
3. **New → Web Service** → connect your repo.
4. **Language**: Docker (auto-detected from the Dockerfile). **Instance Type**: Free.
5. Under **Advanced**:
   - Add `HF_TOKEN` and `CLIENT_API_KEY`.
   - Set **Health Check Path** to `/health`.
6. **Create Web Service**. First build takes a few minutes (installing GDAL).
7. Live at `https://<your-service-name>.onrender.com`.

Same free-tier cold-start tradeoff as before applies here too: after ~15
minutes idle, the first request wakes the instance back up (tens of
seconds). Every request after that is fast.

## 📡 Endpoints

```
GET /health
    → { "status": "ok" }   (unauthenticated, used as Render's Health Check Path)

GET /api/v1/manifest
    → { crs, nodata, tiles: [{ filename, bounds, url }, ...] }
    Tells the client which tiles exist, their bounding boxes, and the exact
    gateway URL to fetch each one through. Look here first to figure out
    which tile(s) cover your area of interest.

GET /tiles/{filename}
    → raw bytes (206 Partial Content)
    Requires an explicit 'Range: bytes=start-end' header. This is how
    GDAL/rasterio/geotiff.js already read COGs by default — no special
    client-side logic needed beyond adding the X-API-Key header.
```

## 🧰 How clients actually use this

### Any GDAL-based tool (rasterio, QGIS, `gdalinfo`, etc.)

Point `/vsicurl/` straight at the gateway URL, with your API key passed as
a custom HTTP header instead of the usual bearer token:

```python
import rasterio

gdal_opts = {"GDAL_HTTP_HEADERS": "X-API-Key: <your key>"}
with rasterio.Env(**gdal_opts):
    with rasterio.open("/vsicurl/https://<your-service>.onrender.com/tiles/TRI_Tile1_COG.tif") as src:
        ...  # read windows exactly like any other remote COG
```

See `client_example/read_via_gateway.py` for a runnable version of this.

### Browser JS (geotiff.js)

```js
const tiff = await GeoTIFF.fromUrl(
  "https://<your-service>.onrender.com/tiles/TRI_Tile1_COG.tif",
  { headers: { "X-API-Key": "<your key>" } }
);
const image = await tiff.getImage();
const rasters = await image.readRasters({ window: [x0, y0, x1, y1] });
```

See `client_example/browser_read_test.html` for a runnable version — open
it directly in a browser after editing the URL/key at the top.

For an actual interactive map (not just a read test), pair this with
[`georaster-layer-for-leaflet`](https://github.com/GeoTIFFjs/georaster-layer-for-leaflet),
which consumes exactly this kind of client-side-fetched raster.

## 🧩 Tile layout & NoData

Same as before: 9 tiles (`TRI_Tile1_COG.tif` … `TRI_Tile9_COG.tif`) in
`J2003S/india-tri`, pre-buffered 300 m on every side so adjacent tiles
overlap cleanly if a client wants to mosaic across a tile boundary itself.
NoData sentinel is `-9999` — `0` is a legitimate flat-terrain value and is
never touched by this gateway (it doesn't inspect pixel values at all).

## ⚙️ CORS

`Access-Control-Allow-Origin` defaults to `*` (override via the
`ALLOWED_ORIGINS` env var, comma-separated, if you want to restrict it).
`Content-Range`, `Content-Length`, and `Accept-Ranges` are explicitly
exposed via CORS — without this, browser JS (including geotiff.js) can't
read those headers even though the request itself succeeds.

## ⚠️ Known limits / next hardening steps

- Usage-quota state is in-memory only (see above) — upgrade to Redis if you
  need it to survive restarts or scale beyond one instance.
- `MAX_RANGE_BYTES` and `DAILY_BYTE_QUOTA_PER_KEY` are starting points —
  tune to your actual commercial terms; there's no dashboard for this yet,
  just the constants at the top of `app.py`.
- No per-minute rate limiting yet, only a daily byte cap — a burst of many
  small requests in a short window isn't currently throttled.
- The gateway doesn't currently log requests anywhere durable — add logging
  before you need to debug a billing dispute or investigate abuse.

## 🚀 Local Development

```bash
pip install -r requirements.txt
export HF_TOKEN="hf_xxxxxxxxxxxxxxxxxxxx"
export CLIENT_API_KEY="dev-key"
export PORT=10000
uvicorn app:app --reload --port $PORT
```

## 📄 License

MIT © 2024 J2003S
