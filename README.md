# MOD35_L2 Daily Cloud Counts

This project creates daily global cloudy-pixel and valid-observation count
GeoTIFFs from NASA MODIS Terra Collection 6.1 data.

The processor combines two products with matching five-minute acquisition
times:

- **MOD35_L2.061** supplies the native 1 km cloud mask.
- **MOD03.061** supplies native 1 km latitude and longitude for every cloud
  mask pixel.

Using MOD03 avoids interpolating the 5 km geolocation tie points embedded in
MOD35_L2. The swaths are mapped to an EPSG:4326 grid with Rasterio/GDAL native
nearest-neighbor resampling.

The script produces daily files only. It does not calculate monthly or annual
statistics.

## Outputs

For each requested date, the script writes two single-band `uint32` GeoTIFFs:

```text
daily/MOD35_cloudy_count_YYYY_MM_DD.tif
daily/MOD35_total_count_YYYY_MM_DD.tif
```

`cloudy_count` is the number of observations classified as cloudy or
uncertain clear (`CLOUDY_CLASSES = [0, 1]`).

`total_count` is the number of observations retained by the configured quality
filters. By default, only determined daytime observations are counted.

The GeoTIFFs use:

- CRS: EPSG:4326
- LZW compression
- Tiled BigTIFF output
- Separate X and Y resolutions
- A processing-version metadata tag used for output validation

With the default settings, the output grid is 40,149 columns by 19,944 rows.

## Requirements

- Python 3.12 or newer
- A free [NASA Earthdata Login](https://urs.earthdata.nasa.gov/)
- Network access to NASA Earthdata and LAADS DAAC
- A current Rasterio release whose `rasterio.warp.reproject()` supports
  `src_geoloc_array`
- HDF4 support for `pyhdf`

Python packages:

```text
earthaccess
numpy
pyhdf
rasterio
tqdm
```

### Recommended installation

Conda is recommended because Rasterio/GDAL and `pyhdf` depend on native
libraries:

```bash
conda create -n mod35-daily -c conda-forge \
  python=3.11 numpy rasterio pyhdf tqdm pip
conda activate mod35-daily
pip install earthaccess
```

## Hardware and storage

The global arrays are disk-backed, but this remains a large-data workflow.
Recommended resources are:

- 16 GB RAM or more
- At least 10 GB free under the directory passed to `--output-dir`
- Additional output space for two compressed BigTIFFs and staging copies
- A fast local disk for the output directory

The two temporary daily arrays contain approximately 6.4 GB of raw `uint32`
data. HDF downloads are streamed and removed after each granule is read.

The script creates `daily/` and `mod35_temp/` below `--output-dir`. A slow
network or object-mounted output directory can significantly reduce performance.

## Configuration

The processing date and base output directory are command-line arguments. Edit
the remaining processing settings near the top of `mod35_daily_tif.py`:

```python
X_RESOLUTION = 0.008966490596076948788
Y_RESOLUTION = 0.009025464499695981177

CLOUDY_CLASSES = [0, 1]
USE_DETERMINED_ONLY = True
DAYTIME_ONLY = True
```

The NASA collection identifiers are already configured:

```python
MOD35_COLLECTION_ID = "C1443561895-LAADS"
MOD03_COLLECTION_ID = "C1379767668-LAADS"
```

## Running

```bash
python mod35_daily.py \
  --proc-date 2025-01-01 \
  --output-dir /home/jovyan/MOD35_L2_0.009deg \
  --earthdata-username "$EARTHDATA_USERNAME" \
  --earthdata-password "$EARTHDATA_PASSWORD"
```

Underscore aliases (`--proc_date` and `--output_dir`) are also accepted. Dates
must use `YYYY-MM-DD` format.

The selected output directory will contain:

```text
MOD35_L2_0.009deg/
├── daily/
│   ├── MOD35_cloudy_count_YYYY_MM_DD.tif
│   └── MOD35_total_count_YYYY_MM_DD.tif
└── mod35_temp/
    ├── *.partial.npy
    └── *.checkpoint.json
```

The first run opens an interactive Earthdata authentication flow. Successful
credentials are persisted by `earthaccess`.

### MAAP DPS

DPS passes the required values positionally:

```bash
./run.sh 2025-01-01 "$EARTHDATA_USERNAME" "$EARTHDATA_PASSWORD"
```

The password is therefore potentially visible in job metadata, process
listings, and diagnostics. Rotate it if it appears in a log or support request.

The FIPS-enabled DPS worker must not use a uv/PyPI virtual environment for this
algorithm. `build-env.sh` installs the runtime packages into the MAAP base
Conda environment so Python, Rasterio/GDAL, HDF4, and OpenSSL come from one
compatible native stack. `run.sh` executes that recorded Conda interpreter
directly. A successful build prints its OpenSSL version and `TLS self-test
passed` after importing all required native packages.

For the requested day, the script:

1. Searches for MOD35_L2 and MOD03 Collection 6.1 granules.
2. Pairs granules by the shared `Ayyyyddd.HHMM` acquisition identifier.
3. Downloads one pair at a time.
4. Decodes the MOD35 cloud mask and applies the configured filters.
5. Resamples both count bands with Rasterio nearest-neighbor warping.
6. Accumulates the results in disk-backed daily arrays.
7. Validates and publishes the two GeoTIFFs.

No daily output is published if a required MOD03 match or granule operation
fails.

## Resume behavior

Completed GeoTIFF pairs with the expected dimensions, data type, CRS,
transform, and processing-version tag are skipped.

During an incomplete day, the script stores temporary arrays and a per-granule
checkpoint in `mod35_temp/` below the selected output directory. After a
controlled failure, rerunning the same date resumes after the last successfully
committed granule pair. Temporary files are removed after the final GeoTIFF pair
is published successfully.

Clearing `mod35_temp/`, changing `PROCESSING_ALGORITHM`, or changing the grid
configuration invalidates that resume state and causes the day to restart.

## Resampling details

Rasterio/GDAL uses MOD03's per-pixel longitude and latitude arrays through
`src_geoloc_array` with `Resampling.nearest`. The implementation also:

- normalizes longitude around each output window to handle the antimeridian;
- divides genuinely polar swaths into bounded row blocks;
- masks invalid MOD03 coordinates;
- keeps bounded GDAL geolocation back-maps in memory;
- commits a granule only after all of its destination windows pass validation.

These steps avoid the curved empty/duplicate-cell artifacts produced by direct
point binning while limiting temporary-file size and memory use.

## Troubleshooting

### `reproject() supports src_geoloc_array` error

Upgrade Rasterio to a current release:

```bash
conda update -c conda-forge rasterio gdal
```

### Earthdata authentication or download errors

Confirm that the Earthdata account is active and that the environment can
reach Earthdata and LAADS DAAC. Delete stale Earthdata credentials only if a
login must be performed again.

### Insufficient temporary storage

Select a fast local `--output-dir` with at least 10 GB free. Do not delete its
`mod35_temp/` checkpoint files if the current day should resume.

### Existing outputs are processed again

The validator intentionally rejects GeoTIFFs created by a different processing
algorithm version. This prevents older artifacted or incomplete files from
being silently reused.

## Data sources

- [MOD35_L2 Collection 6.1](https://doi.org/10.5067/MODIS/MOD35_L2.061)
- [MOD03 Collection 6.1](https://doi.org/10.5067/MODIS/MOD03.061)
- [NASA LAADS DAAC](https://ladsweb.modaps.eosdis.nasa.gov/)

Users are responsible for following NASA Earthdata terms and acknowledging the
source products appropriately in derived datasets and publications.
