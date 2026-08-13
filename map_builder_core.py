"""Core conversion functions for THS2 Map Builder.

Only Python's standard library is used. SAS.Planet cache databases are opened
with SQLite URI mode=ro and are never modified.
"""

from __future__ import annotations

import configparser
from contextlib import closing
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
from typing import Callable, Iterable


Log = Callable[[str], None]


def parse_hlg(path: Path) -> dict:
    parser = configparser.ConfigParser()
    parser.optionxform = str
    with path.open("r", encoding="utf-8-sig") as stream:
        parser.read_file(stream)
    if "HIGHLIGHTING" not in parser:
        raise ValueError("В файле нет секции [HIGHLIGHTING].")
    section = parser["HIGHLIGHTING"]
    points: list[tuple[float, float]] = []
    index = 1
    while f"PointLon_{index}" in section and f"PointLat_{index}" in section:
        points.append(
            (float(section[f"PointLon_{index}"]), float(section[f"PointLat_{index}"]))
        )
        index += 1
    if len(points) < 3:
        raise ValueError("В выделении должно быть не меньше трёх точек.")
    lons = [point[0] for point in points]
    lats = [point[1] for point in points]
    bbox = (min(lons), min(lats), max(lons), max(lats))
    return {"points": points, "bbox": bbox, "sas_zoom": int(section.get("Zoom", "0"))}


def lon_to_x(lon: float, zoom: int) -> int:
    limit = (1 << zoom) - 1
    return min(limit, max(0, int(math.floor((lon + 180.0) / 360.0 * (1 << zoom)))))


def lat_to_y(lat: float, zoom: int) -> int:
    lat = min(85.05112878, max(-85.05112878, lat))
    radians = math.radians(lat)
    value = (1.0 - math.asinh(math.tan(radians)) / math.pi) / 2.0
    limit = (1 << zoom) - 1
    return min(limit, max(0, int(math.floor(value * (1 << zoom)))))


def tile_range(bbox: tuple[float, float, float, float], zoom: int) -> tuple[int, int, int, int]:
    west, south, east, north = bbox
    if west > east:
        raise ValueError("Области через линию 180° пока не поддерживаются.")
    return (
        lon_to_x(west, zoom),
        lon_to_x(east, zoom),
        lat_to_y(north, zoom),
        lat_to_y(south, zoom),
    )


def expected_tiles(bbox: tuple[float, float, float, float], min_zoom: int, max_zoom: int) -> int:
    total = 0
    for zoom in range(min_zoom, max_zoom + 1):
        min_x, max_x, min_y, max_y = tile_range(bbox, zoom)
        total += (max_x - min_x + 1) * (max_y - min_y + 1)
    return total


def cache_db_paths(
    cache_root: Path, bbox: tuple[float, float, float, float], zoom: int
) -> Iterable[Path]:
    min_x, max_x, min_y, max_y = tile_range(bbox, zoom)
    # SAS.Planet's SQLite cache uses its historical 1-based zoom folder.
    zoom_root = cache_root / f"z{zoom + 1}"
    for block_x in range(min_x // 256, max_x // 256 + 1):
        for block_y in range(min_y // 256, max_y // 256 + 1):
            yield (
                zoom_root
                / str((block_x * 256) // 1024)
                / str((block_y * 256) // 1024)
                / f"{block_x}.{block_y}.sqlitedb"
            )


def detect_tile_format(blob: bytes) -> str:
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if blob.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if blob.startswith(b"RIFF") and blob[8:12] == b"WEBP":
        return "webp"
    if len(blob) >= 12 and blob[4:8] == b"ftyp" and blob[8:12] in {
        b"avif",
        b"avis",
    }:
        return "avif"
    raise ValueError("В кэше найден неподдерживаемый формат тайла.")


def x_to_lon(x: int, zoom: int) -> float:
    return x / (1 << zoom) * 360.0 - 180.0


def y_to_lat(y: int, zoom: int) -> float:
    value = math.pi * (1.0 - 2.0 * y / (1 << zoom))
    return math.degrees(math.atan(math.sinh(value)))


def _create_mbtiles(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode=DELETE;
        PRAGMA synchronous=NORMAL;
        CREATE TABLE metadata (name TEXT, value TEXT);
        CREATE UNIQUE INDEX metadata_idx ON metadata (name);
        CREATE TABLE tiles (
            zoom_level INTEGER,
            tile_column INTEGER,
            tile_row INTEGER,
            tile_data BLOB
        );
        CREATE UNIQUE INDEX tiles_idx
            ON tiles (zoom_level, tile_column, tile_row);
        """
    )


def _write_mbtiles_metadata(
    connection: sqlite3.Connection,
    *,
    name: str,
    attribution: str,
    tile_format: str,
    min_zoom: int,
    max_zoom: int,
    bbox: tuple[float, float, float, float],
    description: str,
) -> None:
    west, south, east, north = bbox
    metadata = {
        "name": name,
        "type": "baselayer",
        "version": "1.3",
        "description": description,
        "format": tile_format,
        "scheme": "tms",
        "minzoom": str(min_zoom),
        "maxzoom": str(max_zoom),
        "bounds": f"{west:.8f},{south:.8f},{east:.8f},{north:.8f}",
        "center": f"{(west + east) / 2:.8f},{(south + north) / 2:.8f},{min_zoom}",
        "attribution": attribution.strip(),
    }
    connection.executemany("INSERT INTO metadata VALUES (?, ?)", metadata.items())


def build_mbtiles_from_xyz(
    tiles_root: Path,
    output_path: Path,
    name: str,
    attribution: str,
    log: Log = print,
) -> dict:
    """Build MBTiles from a read-only XYZ directory laid out as z/x/y.ext."""
    if not tiles_root.is_dir():
        raise FileNotFoundError(f"Не найдена папка тайлов: {tiles_root}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()

    target = sqlite3.connect(output_path)
    inserted = 0
    tile_format: str | None = None
    zoom_bounds: dict[int, list[int]] = {}
    try:
        _create_mbtiles(target)
        zoom_folders = sorted(
            (path for path in tiles_root.iterdir() if path.is_dir() and path.name.isdigit()),
            key=lambda path: int(path.name),
        )
        for zoom_folder in zoom_folders:
            zoom = int(zoom_folder.name)
            if not 0 <= zoom <= 24:
                continue
            limit = 1 << zoom
            zoom_count = 0
            bounds = [limit, limit, -1, -1]
            for x_folder in zoom_folder.iterdir():
                if not x_folder.is_dir() or not x_folder.name.isdigit():
                    continue
                x = int(x_folder.name)
                if not 0 <= x < limit:
                    continue
                for tile_path in x_folder.iterdir():
                    if not tile_path.is_file() or not tile_path.stem.isdigit():
                        continue
                    y = int(tile_path.stem)
                    if not 0 <= y < limit:
                        continue
                    blob = tile_path.read_bytes()
                    current_format = detect_tile_format(blob)
                    if tile_format is None:
                        tile_format = current_format
                    elif current_format != tile_format:
                        raise ValueError(
                            "В папке смешаны форматы тайлов. Оставь только один: "
                            "PNG, JPEG, WebP или AVIF."
                        )
                    target.execute(
                        "INSERT OR REPLACE INTO tiles VALUES (?, ?, ?, ?)",
                        (zoom, x, limit - 1 - y, blob),
                    )
                    bounds[0] = min(bounds[0], x)
                    bounds[1] = min(bounds[1], y)
                    bounds[2] = max(bounds[2], x)
                    bounds[3] = max(bounds[3], y)
                    zoom_count += 1
            if zoom_count:
                zoom_bounds[zoom] = bounds
                inserted += zoom_count
                target.commit()
            log(f"Масштаб {zoom}: найдено {zoom_count} тайлов.")

        if not inserted or tile_format is None:
            raise ValueError("В папке не найдены тайлы со структурой z/x/y.png (или jpg/webp/avif).")
        min_zoom = min(zoom_bounds)
        max_zoom = max(zoom_bounds)
        boxes = [
            (
                x_to_lon(bounds[0], zoom),
                y_to_lat(bounds[3] + 1, zoom),
                x_to_lon(bounds[2] + 1, zoom),
                y_to_lat(bounds[1], zoom),
            )
            for zoom, bounds in zoom_bounds.items()
        ]
        bbox = (
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        )
        _write_mbtiles_metadata(
            target,
            name=name or tiles_root.name,
            attribution=attribution,
            tile_format=tile_format,
            min_zoom=min_zoom,
            max_zoom=max_zoom,
            bbox=bbox,
            description="Created by THS2 Map Builder from an XYZ tile directory",
        )
        target.commit()
    except Exception:
        target.close()
        if output_path.exists():
            output_path.unlink()
        raise
    finally:
        try:
            target.close()
        except Exception:
            pass

    return {
        "bbox": bbox,
        "inserted": inserted,
        "format": tile_format,
        "min_zoom": min_zoom,
        "max_zoom": max_zoom,
    }


def _readonly_connection(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    connection.execute("PRAGMA query_only=ON")
    connection.execute("PRAGMA busy_timeout=10000")
    return connection


def build_mbtiles_from_cache(
    cache_root: Path,
    selection_path: Path,
    output_path: Path,
    min_zoom: int,
    max_zoom: int,
    name: str,
    attribution: str,
    log: Log = print,
) -> dict:
    if not cache_root.is_dir():
        raise FileNotFoundError(f"Не найдена папка кэша: {cache_root}")
    selection = parse_hlg(selection_path)
    bbox = selection["bbox"]
    expected = expected_tiles(bbox, min_zoom, max_zoom)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()

    target = sqlite3.connect(output_path)
    inserted = 0
    source_databases = 0
    tile_format: str | None = None
    try:
        target.executescript(
            """
            PRAGMA journal_mode=DELETE;
            PRAGMA synchronous=NORMAL;
            CREATE TABLE metadata (name TEXT, value TEXT);
            CREATE UNIQUE INDEX metadata_idx ON metadata (name);
            CREATE TABLE tiles (
                zoom_level INTEGER,
                tile_column INTEGER,
                tile_row INTEGER,
                tile_data BLOB
            );
            CREATE UNIQUE INDEX tiles_idx
                ON tiles (zoom_level, tile_column, tile_row);
            """
        )
        for zoom in range(min_zoom, max_zoom + 1):
            min_x, max_x, min_y, max_y = tile_range(bbox, zoom)
            zoom_count = 0
            for database in cache_db_paths(cache_root, bbox, zoom):
                if not database.exists():
                    continue
                source_databases += 1
                with closing(_readonly_connection(database)) as source:
                    rows = source.execute(
                        """
                        SELECT x, y, b FROM t
                        WHERE v = 0 AND x BETWEEN ? AND ? AND y BETWEEN ? AND ?
                          AND b IS NOT NULL
                        """,
                        (min_x, max_x, min_y, max_y),
                    )
                    for x, xyz_y, blob in rows:
                        current_format = detect_tile_format(blob)
                        if tile_format is None:
                            tile_format = current_format
                        elif current_format != tile_format:
                            raise ValueError(
                                "В выбранной области смешаны форматы тайлов; "
                                "собери карту из одного источника."
                            )
                        tms_y = (1 << zoom) - 1 - xyz_y
                        target.execute(
                            "INSERT OR REPLACE INTO tiles VALUES (?, ?, ?, ?)",
                            (zoom, x, tms_y, blob),
                        )
                        zoom_count += 1
            inserted += zoom_count
            log(f"Масштаб {zoom}: найдено {zoom_count} тайлов.")
            target.commit()

        if inserted == 0 or tile_format is None:
            raise ValueError("В выделенной области и диапазоне масштабов тайлы не найдены.")
        west, south, east, north = bbox
        metadata = {
            "name": name or output_path.stem,
            "type": "baselayer",
            "version": "1.3",
            "description": "Created by THS2 Map Builder from SAS.Planet read-only cache",
            "format": tile_format,
            "scheme": "tms",
            "minzoom": str(min_zoom),
            "maxzoom": str(max_zoom),
            "bounds": f"{west:.8f},{south:.8f},{east:.8f},{north:.8f}",
            "center": f"{(west + east) / 2:.8f},{(south + north) / 2:.8f},{min_zoom}",
            "attribution": attribution.strip(),
        }
        target.executemany("INSERT INTO metadata VALUES (?, ?)", metadata.items())
        target.commit()
    except Exception:
        target.close()
        if output_path.exists():
            output_path.unlink()
        raise
    finally:
        try:
            target.close()
        except Exception:
            pass

    return {
        "bbox": bbox,
        "expected": expected,
        "inserted": inserted,
        "missing": max(0, expected - inserted),
        "source_databases": source_databases,
        "format": tile_format,
    }


def find_pmtiles_cli(explicit_path: str | None = None) -> Path:
    candidates = []
    if explicit_path:
        candidates.append(Path(explicit_path))
    local = Path(__file__).resolve().parent / "bin" / "pmtiles.exe"
    candidates.append(local)
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        if folder:
            candidates.append(Path(folder) / "pmtiles.exe")
            candidates.append(Path(folder) / "pmtiles")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        "Не найден официальный PMTiles CLI. Запусти Install-PmTiles.ps1 "
        "из папки THS2 Map Builder."
    )


def run_pmtiles(
    cli: Path, arguments: list[str], log: Log = print, check: bool = True
) -> subprocess.CompletedProcess[str]:
    command = [str(cli), *arguments]
    result = subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    for line in (result.stdout + "\n" + result.stderr).splitlines():
        if line.strip():
            clean_line = line.strip()
            try:
                log(clean_line)
            except UnicodeEncodeError:
                # Legacy Windows consoles may use cp1251 and reject the CLI's
                # Unicode progress bar. The GUI itself accepts the original.
                log(clean_line.encode("ascii", "replace").decode("ascii"))
    if check and result.returncode != 0:
        raise RuntimeError(f"PMTiles CLI завершился с кодом {result.returncode}.")
    return result


def find_gdal_tools(explicit_folder: str | None = None) -> dict[str, Path]:
    """Find the GDAL command line tools used for georeferenced rasters."""
    folders: list[Path] = []
    if explicit_folder:
        folders.append(Path(explicit_folder))
    configured = os.environ.get("GDAL_HOME", "").strip()
    if configured:
        configured_path = Path(configured)
        folders.extend((configured_path, configured_path / "bin"))
    folders.extend(
        (
            Path(sys.executable).resolve().parent / "bin" / "gdal",
            Path(__file__).resolve().parent / "bin" / "gdal",
            Path(r"C:\OSGeo4W\bin"),
        )
    )
    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    folders.extend(sorted(program_files.glob("QGIS*\\bin"), reverse=True))

    names = ("gdalinfo", "gdalwarp", "gdal_translate", "gdaladdo")
    result: dict[str, Path] = {}
    for name in names:
        executable = f"{name}.exe" if os.name == "nt" else name
        for folder in folders:
            candidate = folder / executable
            if candidate.is_file():
                result[name] = candidate
                break
        if name not in result:
            found = shutil.which(executable) or shutil.which(name)
            if found:
                result[name] = Path(found)
    missing = [name for name in names if name not in result]
    if missing:
        raise FileNotFoundError(
            "Для GeoTIFF и KMZ нужен GDAL. Установи OSGeo4W/QGIS или положи "
            "утилиты GDAL в папку bin\\gdal рядом с приложением. Не найдены: "
            + ", ".join(missing)
        )
    return result


def run_external(
    executable: Path,
    arguments: list[str],
    log: Log = print,
    emit_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PATH"] = str(executable.parent) + os.pathsep + env.get("PATH", "")
    result = subprocess.run(
        [str(executable), *arguments],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        env=env,
    )
    if emit_output:
        for line in (result.stdout + "\n" + result.stderr).splitlines():
            if line.strip():
                log(line.strip())
    if result.returncode != 0:
        raise RuntimeError(
            f"{executable.name} завершился с кодом {result.returncode}. "
            "Проверь географическую привязку исходного файла."
        )
    return result


def build_mbtiles_from_raster(
    input_path: Path,
    output_path: Path,
    min_zoom: int,
    max_zoom: int,
    name: str,
    attribution: str,
    tile_format: str = "PNG",
    jpeg_quality: int = 85,
    gdal_folder: str | None = None,
    log: Log = print,
) -> dict:
    """Reproject a GeoTIFF/KMZ raster to Web Mercator and create MBTiles."""
    if not input_path.is_file():
        raise FileNotFoundError(f"Не найден исходный растр: {input_path}")
    if not 0 <= min_zoom <= max_zoom <= 22:
        raise ValueError("Для георастра масштабы должны быть в диапазоне 0–22.")
    tile_format = tile_format.upper()
    if tile_format not in {"PNG", "JPEG"}:
        raise ValueError("Для георастра доступны тайлы PNG или JPEG.")
    if not 1 <= jpeg_quality <= 100:
        raise ValueError("Качество JPEG должно быть от 1 до 100.")

    tools = find_gdal_tools(gdal_folder)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()
    info = run_external(
        tools["gdalinfo"], ["-json", str(input_path)], log, emit_output=False
    )
    try:
        info_payload = json.loads(info.stdout)
    except json.JSONDecodeError as error:
        raise ValueError("GDAL не смог прочитать сведения о геопривязке.") from error
    if not info_payload.get("coordinateSystem") and not info_payload.get("gcps"):
        raise ValueError(
            "У растра не найдена система координат. Для обычного JPG/PNG сначала "
            "нужно выполнить географическую привязку."
        )

    resolution = 156543.03392804097 / (1 << max_zoom)
    with tempfile.TemporaryDirectory(prefix="ths2-map-builder-raster-") as folder:
        warped = Path(folder) / "web-mercator.vrt"
        warp_arguments = [
            "-overwrite", "-of", "VRT", "-t_srs", "EPSG:3857",
            "-tr", f"{resolution:.12f}", f"{resolution:.12f}",
            "-tap", "-r", "bilinear",
        ]
        if tile_format == "PNG":
            warp_arguments.append("-dstalpha")
        warp_arguments.extend((str(input_path), str(warped)))
        log(f"Перепроецирование в Web Mercator, максимальный масштаб {max_zoom}…")
        run_external(tools["gdalwarp"], warp_arguments, log)

        translate_arguments = [
            "-of", "MBTILES", "-co", f"TILE_FORMAT={tile_format}",
            "-co", "TYPE=baselayer", "-co", f"NAME={name or input_path.stem}",
            "-co", "ZOOM_LEVEL_STRATEGY=LOWER",
        ]
        if tile_format == "JPEG":
            translate_arguments.extend(("-co", f"QUALITY={jpeg_quality}"))
        translate_arguments.extend((str(warped), str(output_path)))
        log("Нарезка растра на тайлы…")
        run_external(tools["gdal_translate"], translate_arguments, log)

        if min_zoom < max_zoom:
            overview_factors = [str(1 << step) for step in range(1, max_zoom - min_zoom + 1)]
            log(f"Создание масштабов {min_zoom}–{max_zoom}…")
            run_external(
                tools["gdaladdo"],
                ["-r", "average", str(output_path), *overview_factors],
                log,
            )

    connection = sqlite3.connect(output_path)
    try:
        zoom_row = connection.execute(
            "SELECT MIN(zoom_level), MAX(zoom_level), COUNT(*) FROM tiles"
        ).fetchone()
        if not zoom_row or zoom_row[2] == 0:
            raise ValueError("GDAL не создал ни одного тайла.")
        actual_min, actual_max, tile_count = zoom_row
        first_blob = connection.execute("SELECT tile_data FROM tiles LIMIT 1").fetchone()[0]
        detected_format = detect_tile_format(first_blob)
        for key, value in (
            ("name", name or input_path.stem),
            ("attribution", attribution.strip()),
            ("type", "baselayer"),
            ("minzoom", str(actual_min)),
            ("maxzoom", str(actual_max)),
            ("format", detected_format),
            ("description", "Created by THS2 Map Builder from a georeferenced raster"),
        ):
            connection.execute("DELETE FROM metadata WHERE name = ?", (key,))
            connection.execute("INSERT INTO metadata(name, value) VALUES (?, ?)", (key, value))
        bounds_row = connection.execute(
            "SELECT value FROM metadata WHERE name = 'bounds' LIMIT 1"
        ).fetchone()
        connection.commit()
    finally:
        connection.close()
    return {
        "source": str(input_path),
        "inserted": tile_count,
        "format": detected_format,
        "min_zoom": actual_min,
        "max_zoom": actual_max,
        "bbox": bounds_row[0] if bounds_row else None,
        "gdal": info_payload.get("description", input_path.name),
    }


def convert_mbtiles(
    input_path: Path,
    output_path: Path,
    cli: Path,
    log: Log = print,
    name: str | None = None,
    attribution: str | None = None,
) -> dict:
    if not input_path.is_file():
        raise FileNotFoundError(f"Не найден MBTiles: {input_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()
    conversion_input = input_path
    temporary: tempfile.TemporaryDirectory[str] | None = None
    if name is not None or attribution is not None:
        temporary = tempfile.TemporaryDirectory(prefix="ths2-map-builder-metadata-")
        conversion_input = Path(temporary.name) / input_path.name
        shutil.copyfile(input_path, conversion_input)
        metadata_connection = sqlite3.connect(conversion_input)
        try:
            for key, value in (("name", name), ("attribution", attribution)):
                if value is None:
                    continue
                metadata_connection.execute("DELETE FROM metadata WHERE name = ?", (key,))
                metadata_connection.execute(
                    "INSERT INTO metadata(name, value) VALUES (?, ?)", (key, value.strip())
                )
            metadata_connection.commit()
        finally:
            metadata_connection.close()
    try:
        run_pmtiles(cli, ["convert", str(conversion_input), str(output_path)], log)
    finally:
        if temporary is not None:
            temporary.cleanup()
    run_pmtiles(cli, ["verify", str(output_path)], log)
    return read_pmtiles_header(output_path)


def read_pmtiles_header(path: Path) -> dict:
    with path.open("rb") as stream:
        header = stream.read(127)
    if len(header) != 127 or header[:7] != b"PMTiles" or header[7] != 3:
        raise ValueError("Файл не является PMTiles v3.")
    values = struct.unpack_from("<11Q", header, 8)
    return {
        "version": header[7],
        "addressed_tiles": values[8],
        "tile_entries": values[9],
        "tile_contents": values[10],
        "tile_type": header[99],
        "min_zoom": header[100],
        "max_zoom": header[101],
        "size": path.stat().st_size,
    }


def write_report(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def cache_to_pmtiles(
    cache_root: Path,
    selection_path: Path,
    output_path: Path,
    min_zoom: int,
    max_zoom: int,
    name: str,
    attribution: str,
    cli: Path,
    log: Log = print,
) -> dict:
    with tempfile.TemporaryDirectory(prefix="ths2-map-builder-") as temp_folder:
        mbtiles_path = Path(temp_folder) / "cache-export.mbtiles"
        cache_result = build_mbtiles_from_cache(
            cache_root,
            selection_path,
            mbtiles_path,
            min_zoom,
            max_zoom,
            name,
            attribution,
            log,
        )
        pmtiles_result = convert_mbtiles(mbtiles_path, output_path, cli, log)
    result = {
        "status": "ok",
        "source": "sasplanet-cache-read-only",
        "output": str(output_path),
        "cache": cache_result,
        "pmtiles": pmtiles_result,
    }
    write_report(output_path.with_suffix(".report.json"), result)
    return result


def xyz_to_pmtiles(
    tiles_root: Path,
    output_path: Path,
    name: str,
    attribution: str,
    cli: Path,
    log: Log = print,
) -> dict:
    with tempfile.TemporaryDirectory(prefix="ths2-map-builder-xyz-") as temp_folder:
        mbtiles_path = Path(temp_folder) / "xyz-export.mbtiles"
        source_result = build_mbtiles_from_xyz(
            tiles_root, mbtiles_path, name, attribution, log
        )
        pmtiles_result = convert_mbtiles(mbtiles_path, output_path, cli, log)
    result = {
        "status": "ok",
        "source_type": "xyz-directory-read-only",
        "source": str(tiles_root),
        "output": str(output_path),
        "tiles": source_result,
        "pmtiles": pmtiles_result,
        "attribution_notice": attribution.strip(),
    }
    write_report(output_path.with_suffix(".report.json"), result)
    return result


def raster_to_pmtiles(
    input_path: Path,
    output_path: Path,
    min_zoom: int,
    max_zoom: int,
    name: str,
    attribution: str,
    cli: Path,
    tile_format: str = "PNG",
    jpeg_quality: int = 85,
    gdal_folder: str | None = None,
    log: Log = print,
) -> dict:
    with tempfile.TemporaryDirectory(prefix="ths2-map-builder-raster-") as temp_folder:
        mbtiles_path = Path(temp_folder) / "raster-export.mbtiles"
        source_result = build_mbtiles_from_raster(
            input_path,
            mbtiles_path,
            min_zoom,
            max_zoom,
            name,
            attribution,
            tile_format,
            jpeg_quality,
            gdal_folder,
            log,
        )
        pmtiles_result = convert_mbtiles(mbtiles_path, output_path, cli, log)
    result = {
        "status": "ok",
        "source_type": "georeferenced-raster",
        "source": str(input_path),
        "output": str(output_path),
        "raster": source_result,
        "pmtiles": pmtiles_result,
        "attribution_notice": attribution.strip(),
    }
    write_report(output_path.with_suffix(".report.json"), result)
    return result
