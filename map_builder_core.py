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
