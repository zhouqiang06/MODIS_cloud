#!/usr/bin/env python3

"""
MOD35_L2 Collection 6.1 daily cloud-count processing with MOD03 geolocation.

For each requested day, this script writes exactly two persistent outputs:
    MOD35_cloudy_count_YYYY_MM_DD.tif
    MOD35_total_count_YYYY_MM_DD.tif

MOD35_L2 supplies the native 1 km cloud mask. Its matching MOD03 Collection
6.1 granule supplies native 1 km latitude/longitude for every cloud-mask
pixel, avoiding interpolation of MOD35's embedded 5 km geolocation tie points.
Monthly and annual statistics are not calculated. Temporary NumPy memory maps
are deleted after the two GeoTIFFs have been published successfully.
"""

import argparse
import gc
import json
import os
import re
import shutil
import tempfile
import warnings
from datetime import datetime, timedelta

import earthaccess
import numpy as np
import rasterio
from pyhdf.SD import SD, SDC
from rasterio.transform import Affine
from rasterio.windows import Window
from tqdm import tqdm


# ============================================================
# USER SETTINGS
# ============================================================

MOD35_COLLECTION_ID = "C1443561895-LAADS"  # Cloud mask, Collection 6.1
MOD03_COLLECTION_ID = "C1379767668-LAADS"  # 1 km geolocation, Collection 6.1

# Initialized from --output-dir by configure_directories().
DAILY_DIR = None
TEMP_DIR = None

# Pixel size in degrees. X and Y may use different resolutions.
X_RESOLUTION = 0.008966490596076948788
Y_RESOLUTION = 0.009025464499695981177

LON_MIN = -180.0
LON_MAX = 180.0
LAT_MIN = -90.0
LAT_MAX = 90.0

CLOUDY_CLASSES = [0, 1]
USE_DETERMINED_ONLY = True
DAYTIME_ONLY = True

# Rows initialized or written at one time. Increase only if RAM allows it.
CHUNK_ROWS = 256

# Rasterio/GDAL warp settings. WINDOW_BUFFER_PIXELS only enlarges the cropped
# destination window; it does not change Rasterio's nearest-neighbor method.
WINDOW_BUFFER_PIXELS = 4
WARP_MEMORY_LIMIT_MB = 512
WARP_THREADS = max(1, min(8, os.cpu_count() or 1))
GEOLOC_BACKMAP_OVERSAMPLE_FACTOR = 1.0
POLAR_GEOLOCATION_CHUNK_ROWS = 256
POLAR_LONGITUDE_SPAN_THRESHOLD = 180.0
PROCESSING_ALGORITHM = "mod03_1km_rasterio_geoloc_nearest_v7"


def configure_directories(save_dir):
    """Configure persistent and temporary paths from one output directory."""
    global DAILY_DIR, TEMP_DIR

    output_dir = os.path.abspath(
        os.path.expanduser(os.fspath(save_dir))
    )
    DAILY_DIR = os.path.join(output_dir, "daily")
    TEMP_DIR = os.path.join(output_dir, "mod35_temp")

    os.makedirs(DAILY_DIR, exist_ok=True)
    os.makedirs(TEMP_DIR, exist_ok=True)

    return output_dir


# ============================================================
# GRID
# ============================================================

n_lon = int(round((LON_MAX - LON_MIN) / X_RESOLUTION))
n_lat = int(round((LAT_MAX - LAT_MIN) / Y_RESOLUTION))
GRID_SHAPE = (n_lat, n_lon)
WGS84_CRS = rasterio.crs.CRS.from_epsg(4326)
GRID_TRANSFORM = Affine.from_gdal(
    LON_MIN,
    X_RESOLUTION,
    0.0,
    LAT_MAX,
    0.0,
    -Y_RESOLUTION,
)


# ============================================================
# FILE PATHS AND VALIDATION
# ============================================================

def daily_paths(date):
    date_text = date.strftime("%Y_%m_%d")
    cloudy = os.path.join(
        DAILY_DIR,
        f"MOD35_cloudy_count_{date_text}.tif",
    )
    total = os.path.join(
        DAILY_DIR,
        f"MOD35_total_count_{date_text}.tif",
    )
    return cloudy, total


def daily_temp_paths(date):
    date_text = date.strftime("%Y_%m_%d")
    cloudy = os.path.join(
        TEMP_DIR,
        f"MOD35_cloudy_count_{date_text}.partial.npy",
    )
    total = os.path.join(
        TEMP_DIR,
        f"MOD35_total_count_{date_text}.partial.npy",
    )
    return cloudy, total


def daily_checkpoint_path(date):
    date_text = date.strftime("%Y_%m_%d")
    return os.path.join(TEMP_DIR, f"MOD35_{date_text}.checkpoint.json")


def valid_partial_array(path):
    if not os.path.isfile(path):
        return False
    try:
        array = np.load(path, mmap_mode="r", allow_pickle=False)
        valid = array.shape == GRID_SHAPE and array.dtype == np.uint32
        del array
        return valid
    except Exception:
        return False


def save_checkpoint(path, completed_keys):
    temporary_path = path + ".tmp"
    with open(temporary_path, "w", encoding="utf-8") as checkpoint_file:
        json.dump(
            {
                "processing_algorithm": PROCESSING_ALGORITHM,
                "completed_keys": sorted(completed_keys),
            },
            checkpoint_file,
        )
        checkpoint_file.flush()
        os.fsync(checkpoint_file.fileno())
    os.replace(temporary_path, path)


def load_checkpoint(path):
    with open(path, "r", encoding="utf-8") as checkpoint_file:
        checkpoint = json.load(checkpoint_file)
    if checkpoint.get("processing_algorithm") != PROCESSING_ALGORITHM:
        raise ValueError("Checkpoint was created by a different algorithm.")
    return set(checkpoint.get("completed_keys", []))


def valid_daily_tif(path):
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        return False

    try:
        with rasterio.open(path) as dataset:
            return (
                dataset.driver == "GTiff"
                and dataset.count == 1
                and dataset.shape == GRID_SHAPE
                and dataset.dtypes == ("uint32",)
                and dataset.crs == WGS84_CRS
                and dataset.transform.almost_equals(GRID_TRANSFORM)
                and dataset.tags().get("PROCESSING_ALGORITHM")
                == PROCESSING_ALGORITHM
            )
    except Exception as exc:
        warnings.warn(f"Invalid daily GeoTIFF {path}: {exc}")
        return False


def valid_daily_pair(date):
    cloudy_file, total_file = daily_paths(date)
    return valid_daily_tif(cloudy_file) and valid_daily_tif(total_file)


# ============================================================
# GRANULE NAME, DATE, AND DOWNLOAD
# ============================================================

def granule_data_links(granule):
    try:
        return granule.data_links()
    except TypeError:
        return granule.data_links(access="external")


def granule_filename(granule):
    title = getattr(granule, "title", None)
    if title:
        title_name = os.path.basename(str(title))
        if title_name.lower().endswith(".hdf"):
            return title_name

    links = granule_data_links(granule)
    if not links:
        raise ValueError("Granule has no downloadable data links.")

    for link in links:
        filename = os.path.basename(str(link).split("?", 1)[0])
        if filename.lower().endswith(".hdf"):
            return filename

    raise ValueError("Granule has no HDF data link.")


def granule_acquisition_key(granule):
    """Return YYYY-DDD-HHMM, shared by matching MOD35_L2 and MOD03 files."""
    filename = granule_filename(granule)
    match = re.search(r"\.A(\d{4})(\d{3})\.(\d{4})\.", filename)
    if not match:
        match = re.search(r"A(\d{4})(\d{3})\.(\d{4})\.", filename)
    if not match:
        raise ValueError(
            f"Cannot parse MODIS acquisition time from filename: {filename}"
        )

    return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"


def granule_date(granule):
    key = granule_acquisition_key(granule)
    year_text, day_text, time_text = key.split("-")
    hour = int(time_text[:2])
    minute = int(time_text[2:])

    return datetime(int(year_text), 1, 1) + timedelta(
        days=int(day_text) - 1,
        hours=hour,
        minutes=minute,
    )


def read_hdf_from_https(granule):
    links = granule_data_links(granule)
    if not links:
        raise ValueError("Granule has no downloadable data links.")

    expected_filename = granule_filename(granule)
    url = next(
        (
            link
            for link in links
            if os.path.basename(str(link).split("?", 1)[0])
            == expected_filename
        ),
        None,
    )
    if url is None:
        raise ValueError(f"No download URL found for {expected_filename}.")

    session = earthaccess.get_requests_https_session()
    temp_path = None

    try:
        with session.get(
            url,
            timeout=(30, 180),
            stream=True,
        ) as response:
            response.raise_for_status()

            with tempfile.NamedTemporaryFile(
                dir=TEMP_DIR,
                suffix=".hdf",
                delete=False,
            ) as temp_file:
                temp_path = temp_file.name
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        temp_file.write(chunk)

        return SD(temp_path, SDC.READ), temp_path

    except Exception:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
        raise


def read_mod35_cloud_mask(granule):
    hdf, temp_path = read_hdf_from_https(granule)

    try:
        cloud_mask = hdf.select("Cloud_Mask").get()
    finally:
        try:
            hdf.end()
        except Exception:
            pass
        if os.path.exists(temp_path):
            os.remove(temp_path)

    return cloud_mask


def read_mod03_geolocation(granule):
    hdf, temp_path = read_hdf_from_https(granule)

    try:
        latitude = hdf.select("Latitude").get()
        longitude = hdf.select("Longitude").get()
    finally:
        try:
            hdf.end()
        except Exception:
            pass
        if os.path.exists(temp_path):
            os.remove(temp_path)

    return latitude, longitude


# ============================================================
# CLOUD MASK, GEOLOCATION, AND GRID RESAMPLING
# ============================================================

def decode_cloud_mask(cloud_mask):
    byte0 = cloud_mask[0].astype(np.int16)
    byte0[byte0 < 0] += 256
    byte0 = byte0.astype(np.uint8)

    determined = (byte0 & 1) == 1
    cloud_class = (byte0 >> 1) & 3
    daytime = ((byte0 >> 3) & 1) == 1
    return cloud_class, determined, daytime


def resample_to_grid(
    latitude,
    longitude,
    cloudy,
    total,
    cloudy_grid,
    total_grid,
):
    """
    Resample a MODIS swath with Rasterio/GDAL's native nearest-neighbor warp.

    The warp uses the per-pixel longitude/latitude arrays as the source
    geolocation model. Destination work is restricted to the swath's rows and
    one or two longitude windows, which keeps memory bounded and handles a
    swath that crosses the antimeridian.
    """
    good_geo = (
        np.isfinite(latitude)
        & np.isfinite(longitude)
        & (latitude >= LAT_MIN)
        & (latitude <= LAT_MAX)
        & (longitude >= LON_MIN)
        & (longitude <= LON_MAX)
    )
    if not np.any(good_geo):
        return

    import inspect

    from rasterio.enums import Resampling
    from rasterio.warp import reproject

    if "src_geoloc_array" not in inspect.signature(reproject).parameters:
        raise RuntimeError(
            "This script requires a Rasterio version whose warp.reproject() "
            "supports src_geoloc_array. Upgrade Rasterio to a current release."
        )

    source_latitude = latitude[good_geo].astype(np.float64, copy=False)
    source_longitude = longitude[good_geo].astype(np.float64, copy=False)

    # The destination crop extends slightly beyond source-center bounds so
    # GDAL can include the full edge-pixel footprint.
    latitude_buffer = WINDOW_BUFFER_PIXELS * Y_RESOLUTION

    # Crop the target rows to the swath plus the window buffer.
    north = min(LAT_MAX, float(np.max(source_latitude)) + latitude_buffer)
    south = max(LAT_MIN, float(np.min(source_latitude)) - latitude_buffer)
    row_start = max(0, int(np.floor((LAT_MAX - north) / Y_RESOLUTION)))
    row_stop = min(
        n_lat,
        int(np.ceil((LAT_MAX - south) / Y_RESOLUTION)) + 1,
    )
    if row_start >= row_stop:
        return

    # Select columns in a circular longitude domain. A dateline-crossing
    # swath then becomes two small, contiguous destination windows rather
    # than one nearly global allocation.
    source_longitude_radians = np.deg2rad(source_longitude)
    mean_sine = float(np.mean(np.sin(source_longitude_radians)))
    mean_cosine = float(np.mean(np.cos(source_longitude_radians)))
    center_longitude = np.rad2deg(np.arctan2(mean_sine, mean_cosine))

    adjusted_source_longitude = center_longitude + (
        (source_longitude - center_longitude + 180.0) % 360.0
    ) - 180.0

    max_absolute_latitude = min(
        89.99,
        float(np.max(np.abs(source_latitude))),
    )
    longitude_buffer = min(
        180.0,
        (WINDOW_BUFFER_PIXELS * X_RESOLUTION)
        / max(np.cos(np.deg2rad(max_absolute_latitude)), 0.001),
    )
    west = float(np.min(adjusted_source_longitude)) - longitude_buffer
    east = float(np.max(adjusted_source_longitude)) + longitude_buffer

    all_columns = np.arange(n_lon, dtype=np.int64)
    all_longitudes = LON_MIN + (all_columns + 0.5) * X_RESOLUTION
    adjusted_target_longitudes = center_longitude + (
        (all_longitudes - center_longitude + 180.0) % 360.0
    ) - 180.0
    candidate_columns = all_columns[
        (adjusted_target_longitudes >= west)
        & (adjusted_target_longitudes <= east)
    ]
    if candidate_columns.size == 0:
        return

    # Split the selected columns wherever the dateline creates a gap.
    split_indices = np.flatnonzero(np.diff(candidate_columns) > 1) + 1
    column_windows = np.split(candidate_columns, split_indices)

    source_latitude_array = np.where(
        good_geo,
        latitude.astype(np.float64, copy=False),
        np.nan,
    )
    source_values = np.stack(
        (
            (cloudy & good_geo).astype(np.uint8, copy=False),
            (total & good_geo).astype(np.uint8, copy=False),
        ),
        axis=0,
    )
    destination_height = row_stop - row_start
    warped_valid_pixel_count = 0
    pending_updates = []

    for columns in column_windows:
        if columns.size == 0:
            continue

        col_start = int(columns[0])
        col_stop = int(columns[-1]) + 1
        destination_width = col_stop - col_start

        # Express every source longitude in the continuous 360-degree branch
        # centered on this destination window. Without this normalization,
        # GDAL sees a dateline-crossing swath as nearly global and may create
        # classic temporary TIFF back-maps that exceed the 4 GB size limit.
        window_center_longitude = LON_MIN + (
            (col_start + col_stop) * 0.5 * X_RESOLUTION
        )
        window_longitude = window_center_longitude + (
            (
                longitude.astype(np.float64, copy=False)
                - window_center_longitude
                + 180.0
            )
            % 360.0
        ) - 180.0
        window_longitude[~good_geo] = np.nan

        finite_longitude = window_longitude[np.isfinite(window_longitude)]
        if finite_longitude.size == 0:
            continue
        window_longitude_span = float(
            np.max(finite_longitude) - np.min(finite_longitude)
        )

        warped_values = np.zeros(
            (2, destination_height, destination_width),
            dtype=np.uint8,
        )

        source_row_count = latitude.shape[0]
        if window_longitude_span > POLAR_LONGITUDE_SPAN_THRESHOLD:
            source_row_slices = []
            for core_start in range(
                0,
                source_row_count,
                POLAR_GEOLOCATION_CHUNK_ROWS,
            ):
                core_end = min(
                    core_start + POLAR_GEOLOCATION_CHUNK_ROWS,
                    source_row_count,
                )
                # One-row overlap prevents a seam between adjacent chunks.
                chunk_start = max(0, core_start - 1)
                chunk_end = min(source_row_count, core_end + 1)
                source_row_slices.append(slice(chunk_start, chunk_end))
        else:
            source_row_slices = [slice(0, source_row_count)]

        with tempfile.TemporaryDirectory(
            dir=TEMP_DIR,
            prefix="gdal_geoloc_",
        ) as gdal_temp_dir:
            with rasterio.Env(
                CPL_TMPDIR=gdal_temp_dir,
                GEOLOC_USE_TEMP_DATASETS="NO",
            ):
                for source_rows in source_row_slices:
                    chunk_source_values = source_values[:, source_rows, :]
                    if not np.any(chunk_source_values[1]):
                        continue

                    chunk_latitude = source_latitude_array[source_rows, :]
                    finite_chunk_latitude = chunk_latitude[
                        np.isfinite(chunk_latitude)
                    ]
                    if finite_chunk_latitude.size == 0:
                        continue

                    chunk_north = min(
                        LAT_MAX,
                        float(np.max(finite_chunk_latitude)) + latitude_buffer,
                    )
                    chunk_south = max(
                        LAT_MIN,
                        float(np.min(finite_chunk_latitude)) - latitude_buffer,
                    )
                    chunk_row_start = max(
                        row_start,
                        int(np.floor((LAT_MAX - chunk_north) / Y_RESOLUTION)),
                    )
                    chunk_row_stop = min(
                        row_stop,
                        int(np.ceil((LAT_MAX - chunk_south) / Y_RESOLUTION)) + 1,
                    )
                    if chunk_row_start >= chunk_row_stop:
                        continue

                    chunk_destination_height = chunk_row_stop - chunk_row_start
                    chunk_destination_window = Window(
                        col_off=col_start,
                        row_off=chunk_row_start,
                        width=destination_width,
                        height=chunk_destination_height,
                    )
                    chunk_destination_transform = rasterio.windows.transform(
                        chunk_destination_window,
                        GRID_TRANSFORM,
                    )
                    chunk_warped_values = np.zeros(
                        (2, chunk_destination_height, destination_width),
                        dtype=np.uint8,
                    )
                    source_geolocation = np.stack(
                        (
                            window_longitude[source_rows, :],
                            chunk_latitude,
                        ),
                        axis=0,
                    )
                    warp_options = {
                        "src_crs": WGS84_CRS,
                        "src_geoloc_array": source_geolocation,
                        "dst_transform": chunk_destination_transform,
                        "dst_crs": WGS84_CRS,
                        "dst_nodata": 0,
                        "resampling": Resampling.nearest,
                        "num_threads": WARP_THREADS,
                        "init_dest_nodata": True,
                        "warp_mem_limit": WARP_MEMORY_LIMIT_MB,
                        "GEOLOC_BACKMAP_OVERSAMPLE_FACTOR": (
                            GEOLOC_BACKMAP_OVERSAMPLE_FACTOR
                        ),
                        "GEOLOC_USE_TEMP_DATASETS": "NO",
                    }
                    reproject(
                        source=chunk_source_values,
                        destination=chunk_warped_values,
                        **warp_options,
                    )

                    parent_start = chunk_row_start - row_start
                    parent_stop = chunk_row_stop - row_start
                    parent_values = warped_values[
                        :,
                        parent_start:parent_stop,
                        :,
                    ]
                    new_valid = chunk_warped_values[1] > 0
                    previously_empty = parent_values[1] == 0
                    replace = new_valid & previously_empty
                    parent_values[0][replace] = chunk_warped_values[0][replace]
                    parent_values[1][replace] = chunk_warped_values[1][replace]

        window_valid_pixel_count = int(np.count_nonzero(warped_values[1]))
        warped_valid_pixel_count += window_valid_pixel_count
        pending_updates.append((col_start, col_stop, warped_values))

    if np.any(total) and warped_valid_pixel_count == 0:
        raise RuntimeError(
            "Rasterio/GDAL produced no valid destination pixels for a swath "
            "that contains valid observations. Check preceding GDAL errors."
        )

    # Commit only after every destination window has completed and the whole
    # granule has passed validation. This prevents partial daily counts if a
    # later dateline window fails.
    for col_start, col_stop, warped_values in pending_updates:
        for block_start in range(0, destination_height, CHUNK_ROWS):
            block_end = min(block_start + CHUNK_ROWS, destination_height)
            output_start = row_start + block_start
            output_end = row_start + block_end
            cloudy_grid[
                output_start:output_end,
                col_start:col_stop,
            ] += warped_values[0, block_start:block_end, :]
            total_grid[
                output_start:output_end,
                col_start:col_stop,
            ] += warped_values[1, block_start:block_end, :]


def process_granule(mod35_granule, mod03_granule, cloudy_grid, total_grid):
    try:
        mod35_key = granule_acquisition_key(mod35_granule)
        mod03_key = granule_acquisition_key(mod03_granule)
        if mod35_key != mod03_key:
            raise ValueError(
                f"MOD35/MOD03 acquisition mismatch: {mod35_key} != {mod03_key}"
            )

        cloud_mask = read_mod35_cloud_mask(mod35_granule)
        latitude, longitude = read_mod03_geolocation(mod03_granule)
        expected_shape = cloud_mask.shape[1:]

        if latitude.shape != expected_shape or longitude.shape != expected_shape:
            raise ValueError(
                "Native MOD03 geolocation shape mismatch: "
                f"cloud mask={expected_shape}, latitude={latitude.shape}, "
                f"longitude={longitude.shape}"
            )

        cloud_class, determined, daytime = decode_cloud_mask(cloud_mask)
        valid = np.ones(cloud_class.shape, dtype=bool)

        if USE_DETERMINED_ONLY:
            valid &= determined
        if DAYTIME_ONLY:
            valid &= daytime

        cloudy = np.isin(cloud_class, CLOUDY_CLASSES) & valid

        if cloudy.shape != latitude.shape or cloudy.shape != longitude.shape:
            raise ValueError("Cloud mask and geolocation shapes differ.")

        resample_to_grid(
            latitude,
            longitude,
            cloudy,
            valid,
            cloudy_grid,
            total_grid,
        )
        return True

    except Exception as exc:
        try:
            cloud_name = granule_filename(mod35_granule)
            geolocation_name = granule_filename(mod03_granule)
        except Exception:
            cloud_name = "unknown MOD35 granule"
            geolocation_name = "unknown MOD03 granule"
        warnings.warn(
            f"Granule pair failed: {cloud_name} + {geolocation_name}\n{exc}"
        )
        return False


# ============================================================
# GEOTIFF WRITING AND SAFE PUBLICATION
# ============================================================

def write_grid_to_geotiff(grid, tif_file):
    profile = {
        "driver": "GTiff",
        "height": GRID_SHAPE[0],
        "width": GRID_SHAPE[1],
        "count": 1,
        "dtype": "uint32",
        "crs": WGS84_CRS,
        "transform": GRID_TRANSFORM,
        "compress": "LZW",
        "predictor": 2,
        "BIGTIFF": "YES",
        "tiled": True,
        "blockxsize": 512,
        "blockysize": 512,
        "SPARSE_OK": "TRUE",
    }

    with rasterio.open(tif_file, "w", **profile) as destination:
        destination.update_tags(
            PROCESSING_ALGORITHM=PROCESSING_ALGORITHM,
            GEOLOCATION_PRODUCT="MOD03.061 (native 1 km)",
            RESAMPLING_METHOD="rasterio.warp.reproject/nearest",
            GEOLOC_BACKMAP_OVERSAMPLE_FACTOR=str(
                GEOLOC_BACKMAP_OVERSAMPLE_FACTOR
            ),
            WINDOW_BUFFER_PIXELS=str(WINDOW_BUFFER_PIXELS),
        )
        for start in range(0, n_lat, CHUNK_ROWS):
            end = min(start + CHUNK_ROWS, n_lat)
            window = Window(
                col_off=0,
                row_off=start,
                width=n_lon,
                height=end - start,
            )
            destination.write(
                np.asarray(grid[start:end, :], dtype=np.uint32),
                1,
                window=window,
            )


def publish_daily_pair(cloudy_grid, total_grid, final_cloudy, final_total):
    destinations = [final_cloudy, final_total]
    staging = [path + ".staging" for path in destinations]
    backups = [path + ".backup" for path in destinations]

    for path in staging + backups:
        if os.path.exists(path):
            os.remove(path)

    backed_up = []
    published = []

    try:
        write_grid_to_geotiff(cloudy_grid, staging[0])
        write_grid_to_geotiff(total_grid, staging[1])

        for path in staging:
            if not valid_daily_tif(path):
                raise IOError(f"Staged GeoTIFF failed validation: {path}")

        for destination, backup in zip(destinations, backups):
            if os.path.exists(destination):
                shutil.copyfile(destination, backup)
                backed_up.append((destination, backup))

        for stage, destination in zip(staging, destinations):
            shutil.copyfile(stage, destination)
            published.append(destination)

        for destination in destinations:
            if not valid_daily_tif(destination):
                raise IOError(f"Published GeoTIFF failed validation: {destination}")

    except Exception:
        for destination in published:
            try:
                if os.path.exists(destination):
                    os.remove(destination)
            except OSError:
                pass

        for destination, backup in backed_up:
            try:
                shutil.copyfile(backup, destination)
            except OSError as exc:
                warnings.warn(f"Could not restore {destination}: {exc}")
        raise

    finally:
        for path in staging + backups:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass


# ============================================================
# DAILY PROCESSING
# ============================================================

def process_day(date, granule_pairs):
    if valid_daily_pair(date):
        temp_cloudy, temp_total = daily_temp_paths(date)
        checkpoint_file = daily_checkpoint_path(date)
        for path in (temp_cloudy, temp_total, checkpoint_file):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        print(f"Skipping completed day {date}")
        return True

    final_cloudy, final_total = daily_paths(date)
    temp_cloudy, temp_total = daily_temp_paths(date)
    checkpoint_file = daily_checkpoint_path(date)

    cloudy_grid = None
    total_grid = None
    keep_partial = False
    completed_keys = set()

    try:
        can_resume = (
            valid_partial_array(temp_cloudy)
            and valid_partial_array(temp_total)
            and os.path.isfile(checkpoint_file)
        )
        if can_resume:
            try:
                completed_keys = load_checkpoint(checkpoint_file)
            except Exception as exc:
                warnings.warn(f"Ignoring invalid daily checkpoint: {exc}")
                can_resume = False

        if can_resume:
            cloudy_grid = np.load(
                temp_cloudy,
                mmap_mode="r+",
                allow_pickle=False,
            )
            total_grid = np.load(
                temp_total,
                mmap_mode="r+",
                allow_pickle=False,
            )
            print(
                f"Resuming {date} after {len(completed_keys):,} "
                "completed granule pairs."
            )
        else:
            for path in (temp_cloudy, temp_total, checkpoint_file):
                if os.path.exists(path):
                    os.remove(path)

            cloudy_grid = np.lib.format.open_memmap(
                temp_cloudy,
                mode="w+",
                dtype=np.uint32,
                shape=GRID_SHAPE,
            )
            total_grid = np.lib.format.open_memmap(
                temp_total,
                mode="w+",
                dtype=np.uint32,
                shape=GRID_SHAPE,
            )
            # The files were just created after prior versions were removed.
            # Newly extended regions read as zero, so a separate 6.4 GB
            # zero-initialization pass is unnecessary.
            save_checkpoint(checkpoint_file, completed_keys)

        keep_partial = True
        remaining_pairs = [
            pair
            for pair in granule_pairs
            if granule_acquisition_key(pair[0]) not in completed_keys
        ]

        failed = 0
        for mod35_granule, mod03_granule in tqdm(
            remaining_pairs,
            desc=f"Processing {date}",
            unit="granule pair",
        ):
            if not process_granule(
                mod35_granule,
                mod03_granule,
                cloudy_grid,
                total_grid,
            ):
                failed += 1
                break

            completed_keys.add(granule_acquisition_key(mod35_granule))
            cloudy_grid.flush()
            total_grid.flush()
            save_checkpoint(checkpoint_file, completed_keys)

        cloudy_grid.flush()
        total_grid.flush()

        if failed:
            warnings.warn(
                f"Day {date} was not published because {failed} "
                "MOD35/MOD03 granule pair(s) failed. It will be retried "
                "on the next run."
            )
            return False

        publish_daily_pair(
            cloudy_grid,
            total_grid,
            final_cloudy,
            final_total,
        )
        keep_partial = False
        print(f"Saved daily GeoTIFFs for {date}")
        return True

    finally:
        if cloudy_grid is not None:
            del cloudy_grid
        if total_grid is not None:
            del total_grid
        gc.collect()

        if not keep_partial:
            for path in (temp_cloudy, temp_total, checkpoint_file):
                try:
                    if os.path.exists(path):
                        os.remove(path)
                except OSError:
                    pass


def main(
    proc_date: str,
    save_dir,
    earthdata_username=None,
    earthdata_password_file=None,
):
    warnings.filterwarnings("ignore", category=FutureWarning)
    output_dir = configure_directories(save_dir)
    requested_date = datetime.strptime(proc_date, "%Y-%m-%d").date()

    print(f"Output directory: {output_dir}")
    print(f"Daily directory: {DAILY_DIR}")
    print(f"Temporary directory: {TEMP_DIR}")
    print(f"Grid dimensions: {n_lon:,} x {n_lat:,}")
    print(f"X resolution: {X_RESOLUTION} degrees")
    print(f"Y resolution: {Y_RESOLUTION} degrees")
    print("Resampling: Rasterio/GDAL native nearest neighbor")
    print("Output: daily cloudy-count and total-count GeoTIFFs only")

    configured_strategy = os.environ.get("EARTHACCESS_LOGIN_STRATEGY")
    environment_username = os.environ.get("EARTHDATA_USERNAME")
    environment_password = os.environ.get("EARTHDATA_PASSWORD")

    effective_username = earthdata_username or environment_username
    effective_password = (
        read_password_file(earthdata_password_file)
        if earthdata_password_file
        else environment_password
    )

    if bool(effective_username) != bool(effective_password):
        raise RuntimeError(
            "Earthdata username and password must either both be provided or "
            "both be omitted. Use environment variables, or combine "
            "--earthdata-username with --earthdata-password-file."
        )

    if configured_strategy:
        login_strategy = configured_strategy.strip().lower()
    elif effective_username and effective_password:
        login_strategy = "environment"
    elif os.path.isfile(os.path.expanduser("~/.netrc")):
        login_strategy = "netrc"
    elif sys.stdin.isatty():
        login_strategy = "interactive"
    else:
        raise RuntimeError(
            "No Earthdata credentials are available in this headless "
            "environment. Set EARTHDATA_USERNAME and EARTHDATA_PASSWORD, "
            "or mount ~/.netrc."
        )

    if login_strategy not in {"interactive", "environment", "netrc"}:
        raise ValueError(
            "EARTHACCESS_LOGIN_STRATEGY must be interactive, environment, "
            f"or netrc; received {login_strategy!r}."
        )
    if login_strategy == "environment" and not (
        effective_username and effective_password
    ):
        raise RuntimeError(
            "Environment authentication requires both an Earthdata username "
            "and password."
        )

    print(f"\nAuthenticating with Earthaccess using {login_strategy!r}...")

    # Earthaccess's environment strategy reads these standard names. Restore
    # the caller's environment immediately after login when credentials came
    # from command-line/file parameters.
    original_username = os.environ.get("EARTHDATA_USERNAME")
    original_password = os.environ.get("EARTHDATA_PASSWORD")
    try:
        if login_strategy == "environment":
            os.environ["EARTHDATA_USERNAME"] = effective_username
            os.environ["EARTHDATA_PASSWORD"] = effective_password
        earthaccess.login(
            strategy=login_strategy,
            persist=(login_strategy == "interactive"),
        )
    finally:
        if original_username is None:
            os.environ.pop("EARTHDATA_USERNAME", None)
        else:
            os.environ["EARTHDATA_USERNAME"] = original_username

        if original_password is None:
            os.environ.pop("EARTHDATA_PASSWORD", None)
        else:
            os.environ["EARTHDATA_PASSWORD"] = original_password

        effective_password = None

    print(f"Searching for MOD35_L2 Collection 6.1 granules on {proc_date}...")
    mod35_results = list(
        earthaccess.search_data(
            concept_id=MOD35_COLLECTION_ID,
            temporal=(
                f"{proc_date}T00:00:00",
                f"{proc_date}T23:59:59",
            ),
            count=-1,
        )
    )
    print(f"Found {len(mod35_results):,} MOD35_L2 search results.")

    if not mod35_results:
        raise RuntimeError(
            f"No MOD35_L2 Collection 6.1 granules were returned for {proc_date}."
        )

    print(f"Searching for native 1 km MOD03 geolocation on {proc_date}...")
    mod03_results = list(
        earthaccess.search_data(
            concept_id=MOD03_COLLECTION_ID,
            temporal=(
                f"{proc_date}T00:00:00",
                f"{proc_date}T23:59:59",
            ),
            count=-1,
        )
    )
    print(f"Found {len(mod03_results):,} MOD03 search results.")

    if not mod03_results:
        raise RuntimeError(
            f"No MOD03 Collection 6.1 granules were returned for {proc_date}."
        )

    def index_granules(results, product_name):
        indexed = {}
        skipped = 0

        for granule in results:
            try:
                filename = granule_filename(granule)
                if not filename.lower().endswith(".hdf"):
                    continue

                date = granule_date(granule).date()
                if date != requested_date:
                    continue

                key = granule_acquisition_key(granule)
                if key in indexed:
                    raise ValueError(
                        f"Duplicate {product_name} acquisition key {key}"
                    )
                indexed[key] = granule
            except Exception as exc:
                skipped += 1
                warnings.warn(f"Skipping unrecognized {product_name} granule: {exc}")

        if skipped:
            print(f"Skipped {skipped:,} unrecognized {product_name} results.")
        return indexed

    mod35_by_key = index_granules(mod35_results, "MOD35_L2")
    mod03_by_key = index_granules(mod03_results, "MOD03")

    missing_mod03 = sorted(set(mod35_by_key) - set(mod03_by_key))
    if missing_mod03:
        raise RuntimeError(
            "Native 1 km MOD03 geolocation is missing for these MOD35_L2 "
            "acquisitions: " + ", ".join(missing_mod03)
        )

    matched_keys = sorted(set(mod35_by_key) & set(mod03_by_key))
    if not matched_keys:
        raise RuntimeError(
            f"No matching MOD35_L2/MOD03 granule pairs were found for {proc_date}."
        )

    granule_pairs = [
        (mod35_by_key[key], mod03_by_key[key])
        for key in matched_keys
    ]
    print(f"Matched {len(granule_pairs):,} MOD35_L2/MOD03 granule pairs.")

    if not process_day(requested_date, granule_pairs):
        raise RuntimeError(
            f"Daily processing for {proc_date} did not produce complete outputs."
        )

    print("\nMOD35_L2 daily GeoTIFF processing finished successfully.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="MOD35_L2 Collection 6.1 daily cloud-count processing."
    )
    parser.add_argument(
        "--proc_date",
        dest="proc_date",
        help="Date in ISO format, for example 2025-01-01.",
        required=True,
    )
    parser.add_argument(
        "--output_dir",
        dest="output_dir",
        help=(
            "Base directory for the daily GeoTIFFs and resumable temporary "
            "files."
        ),
        required=True,
    )
    parser.add_argument(
        "--earthdata_username",
        help=(
            "Earthdata username. Prefer EARTHDATA_USERNAME for automated "
            "runs."
        ),
    )
    parser.add_argument(
        "--earthdata_password_file",
        help=(
            "Path to a one-line password file readable only by its owner. "
            "Raw password command-line arguments are intentionally unsupported."
        ),
    )

    arguments = parser.parse_args()
    main(
        proc_date=arguments.proc_date,
        save_dir=arguments.output_dir,
        earthdata_username=arguments.earthdata_username,
        earthdata_password_file=arguments.earthdata_password_file,
    )
