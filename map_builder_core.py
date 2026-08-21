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
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import struct
import subprocess
import sys
import tempfile
import time
from typing import Callable, Iterable
import xml.etree.ElementTree as ET
import zipfile

from i18n import tr

Log = Callable[[str], None]
Progress = Callable[[str, int | None], None]
GIB = 1024 ** 3
GLOBAL_MAPPER_XML_LIMIT = 256 * 1024
ZIP_MAX_FILES = 2_000_000
ZIP_MAX_UNCOMPRESSED_BYTES = 128 * GIB
ZIP_MAX_MEMBER_BYTES = 1024 * 1024 * 1024
ZIP_MAX_TILE_BYTES = 64 * 1024 * 1024
ZIP_MAX_COMPRESSION_RATIO = 1000
TILE_EXTENSIONS = {
    "png": "png",
    "jpg": "jpg",
    "jpeg": "jpg",
    "webp": "webp",
    "avif": "avif",
}


def working_space_requirements(input_size: int, source_type: str) -> dict[str, int]:
    """Return conservative free-space recommendations for a conversion."""
    input_size = max(0, int(input_size))
    if source_type == "raster":
        temporary = max(10 * GIB, input_size * 8)
        output = max(2 * GIB, input_size * 3)
    elif source_type == "mbtiles":
        temporary = max(4 * GIB, input_size * 3)
        output = max(1 * GIB, int(input_size * 1.25))
    else:
        temporary = max(5 * GIB, input_size * 4)
        output = max(1 * GIB, int(input_size * 1.5))
    return {"temporary": temporary, "output": output}


def format_bytes(value: int) -> str:
    if value >= GIB:
        return tr(f"{value / GIB:.1f} ГБ", f"{value / GIB:.1f} GB")
    return tr(f"{value / 1024 ** 2:.0f} МБ", f"{value / 1024 ** 2:.0f} MB")


def mbtiles_index_status(path: Path) -> dict[str, object]:
    """Inspect whether a tile table (or normalized map table) has a Z/X/Y index."""
    connection = _readonly_connection(path)
    try:
        object_row = connection.execute(
            "SELECT type FROM sqlite_master WHERE name = 'tiles'"
        ).fetchone()
        if not object_row:
            raise ValueError(tr("В MBTiles не найдена таблица или представление tiles.", "The MBTiles file has no tiles table or view."))
        table = "tiles" if object_row[0] == "table" else "map"
        columns = {
            row[1] for row in connection.execute(f"PRAGMA table_info('{table}')")
        }
        required = {"zoom_level", "tile_column", "tile_row"}
        if not required.issubset(columns):
            return {"table": table, "indexed": False, "can_create": False}
        for index_row in connection.execute(f"PRAGMA index_list('{table}')"):
            index_name = str(index_row[1]).replace("'", "''")
            index_columns = [
                row[2]
                for row in connection.execute(f"PRAGMA index_info('{index_name}')")
            ]
            if index_columns[:3] == ["zoom_level", "tile_column", "tile_row"]:
                return {"table": table, "indexed": True, "can_create": True}
        return {"table": table, "indexed": False, "can_create": True}
    finally:
        connection.close()


def ensure_mbtiles_index(path: Path, log: Log = print) -> dict[str, object]:
    """Create a performance index in a writable working copy, never in source."""
    status = mbtiles_index_status(path)
    if status["indexed"]:
        log(tr("Индекс тайлов найден.", "Tile index found."))
        return status
    if not status["can_create"]:
        log(tr("Структура MBTiles нестандартная; автоматический индекс создать нельзя.", "The MBTiles structure is non-standard; an index cannot be created automatically."))
        return status
    table = str(status["table"])
    connection = sqlite3.connect(path)
    try:
        log(tr(f"Создаю индекс {table}(zoom_level, tile_column, tile_row)…", f"Creating {table}(zoom_level, tile_column, tile_row) index…"))
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS ths2_tiles_zxy_idx "
            f"ON {table}(zoom_level, tile_column, tile_row)"
        )
        connection.commit()
    finally:
        connection.close()
    status["indexed"] = True
    status["created"] = True
    log(tr("Индекс рабочей копии создан.", "Working-copy index created."))
    return status


def copy_file_with_progress(
    source: Path,
    destination: Path,
    log: Log = print,
    progress: Progress | None = None,
) -> None:
    total = source.stat().st_size
    copied = 0
    started = time.monotonic()
    if progress:
        progress(tr("Копирование MBTiles", "Copying MBTiles"), 0)
    with source.open("rb") as reader, destination.open("wb") as writer:
        while chunk := reader.read(8 * 1024 * 1024):
            writer.write(chunk)
            copied += len(chunk)
            if progress and total:
                progress(tr("Копирование MBTiles", "Copying MBTiles"), min(100, copied * 100 // total))
    elapsed = max(0.01, time.monotonic() - started)
    log(
        tr(
            f"Рабочая копия создана: {format_bytes(total)} за {elapsed:.1f} с ({format_bytes(int(total / elapsed))}/с).",
            f"Working copy created: {format_bytes(total)} in {elapsed:.1f} s ({format_bytes(int(total / elapsed))}/s).",
        )
    )


def parse_hlg(path: Path) -> dict:
    parser = configparser.ConfigParser()
    parser.optionxform = str
    with path.open("r", encoding="utf-8-sig") as stream:
        parser.read_file(stream)
    if "HIGHLIGHTING" not in parser:
        raise ValueError(tr("В файле нет секции [HIGHLIGHTING].", "The file has no [HIGHLIGHTING] section."))
    section = parser["HIGHLIGHTING"]
    points: list[tuple[float, float]] = []
    index = 1
    while f"PointLon_{index}" in section and f"PointLat_{index}" in section:
        points.append(
            (float(section[f"PointLon_{index}"]), float(section[f"PointLat_{index}"]))
        )
        index += 1
    if len(points) < 3:
        raise ValueError(tr("В выделении должно быть не меньше трёх точек.", "The selection must contain at least three points."))
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
        raise ValueError(tr("Области через линию 180° пока не поддерживаются.", "Areas crossing the 180° meridian are not supported yet."))
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
    raise ValueError(tr("В кэше найден неподдерживаемый формат тайла.", "An unsupported tile format was found in the cache."))


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
    extra: dict[str, str] | None = None,
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
    if extra:
        metadata.update(extra)
    connection.executemany("INSERT INTO metadata VALUES (?, ?)", metadata.items())


def _xyz_index(name: str, prefix: str = "") -> int | None:
    """Parse both plain XYZ names (13/42/17) and z/x/y-prefixed names."""
    value = name
    if prefix and value[:1].lower() == prefix:
        value = value[1:]
    return int(value) if value.isdigit() else None


def _xyz_zoom_folders(tiles_root: Path) -> list[tuple[int, Path]]:
    folders: list[tuple[int, Path]] = []
    for path in tiles_root.iterdir():
        if path.is_dir():
            zoom = _xyz_index(path.name, "z")
            if zoom is not None and 0 <= zoom <= 24:
                folders.append((zoom, path))
    return sorted(folders, key=lambda item: item[0])


def _iter_xyz_tiles(zoom: int, zoom_folder: Path) -> Iterable[tuple[int, int, Path]]:
    limit = 1 << zoom
    for x_folder in zoom_folder.iterdir():
        if not x_folder.is_dir():
            continue
        x = _xyz_index(x_folder.name, "x")
        if x is None or not 0 <= x < limit:
            continue
        for tile_path in x_folder.iterdir():
            if tile_path.is_file():
                y = _xyz_index(tile_path.stem, "y")
                if y is not None and 0 <= y < limit:
                    yield x, y, tile_path


def scan_xyz_directory(
    tiles_root: Path,
    log: Log = print,
    progress: Progress | None = None,
) -> dict[str, object]:
    """Count an XYZ tree without retaining a potentially huge file manifest."""
    if not tiles_root.is_dir():
        raise FileNotFoundError(tr(f"Не найдена папка тайлов: {tiles_root}", f"Tile folder not found: {tiles_root}"))
    global_mapper_scan = scan_global_mapper_directory(
        tiles_root, log, progress, allow_absent=True
    )
    if global_mapper_scan is not None:
        return global_mapper_scan
    zoom_folders = _xyz_zoom_folders(tiles_root)
    if not zoom_folders:
        raise ValueError(tr("Не найдены папки масштабов XYZ. Поддерживаются имена 13 или z13.", "No XYZ zoom folders found. Folder names such as 13 or z13 are supported."))

    total_tiles = 0
    total_bytes = 0
    per_zoom: dict[int, int] = {}
    started = time.monotonic()
    last_update = started
    if progress:
        progress(tr("Поиск тайлов XYZ — 0 найдено", "Scanning XYZ tiles — 0 found"), None)
    for index, (zoom, zoom_folder) in enumerate(zoom_folders, start=1):
        zoom_count = 0
        zoom_bytes = 0
        for _x, _y, tile_path in _iter_xyz_tiles(zoom, zoom_folder):
            zoom_count += 1
            try:
                zoom_bytes += tile_path.stat().st_size
            except OSError as error:
                raise OSError(tr(f"Не удалось прочитать сведения о тайле {tile_path}: {error}", f"Could not read tile information for {tile_path}: {error}")) from error
            now = time.monotonic()
            if progress and (zoom_count % 1000 == 0 or now - last_update >= 0.5):
                progress(
                    tr(f"Поиск тайлов XYZ — найдено {total_tiles + zoom_count:,}", f"Scanning XYZ tiles — {total_tiles + zoom_count:,} found"),
                    None,
                )
                last_update = now
        if zoom_count:
            per_zoom[zoom] = zoom_count
            total_tiles += zoom_count
            total_bytes += zoom_bytes
        log(tr(f"Поиск XYZ, масштаб {zoom}: {zoom_count} тайлов, {format_bytes(zoom_bytes)}.", f"XYZ scan, zoom {zoom}: {zoom_count} tiles, {format_bytes(zoom_bytes)}."))
        if progress:
            progress(
                tr(f"Поиск тайлов XYZ — найдено {total_tiles:,}", f"Scanning XYZ tiles — {total_tiles:,} found"),
                None if index < len(zoom_folders) else 100,
            )

    if not total_tiles:
        raise ValueError(tr(
            "В папке не найдены тайлы со структурой z/x/y.png или zZ/xX/yY.png (также поддерживаются jpg/webp/avif).",
            "No tiles with a z/x/y.png or zZ/xX/yY.png structure were found (jpg/webp/avif are also supported).",
        ))
    elapsed = max(0.01, time.monotonic() - started)
    log(tr(
        f"Поиск XYZ завершён: {total_tiles:,} тайлов, {format_bytes(total_bytes)} за {elapsed:.1f} с.",
        f"XYZ scan completed: {total_tiles:,} tiles, {format_bytes(total_bytes)} in {elapsed:.1f} s.",
    ))
    return {
        "tiles": total_tiles,
        "bytes": total_bytes,
        "zooms": per_zoom,
        "elapsed": elapsed,
    }


def build_mbtiles_from_xyz(
    tiles_root: Path,
    output_path: Path,
    name: str,
    attribution: str,
    log: Log = print,
    progress: Progress | None = None,
    scan_result: dict[str, object] | None = None,
) -> dict:
    """Build MBTiles from a read-only XYZ directory laid out as z/x/y.ext."""
    if not tiles_root.is_dir():
        raise FileNotFoundError(tr(f"Не найдена папка тайлов: {tiles_root}", f"Tile folder not found: {tiles_root}"))
    scan_result = scan_result or scan_xyz_directory(tiles_root, log, progress)
    if scan_result.get("layout") == "global-mapper-z-y-x":
        return build_mbtiles_from_global_mapper_directory(
            tiles_root,
            output_path,
            name,
            attribution,
            log,
            progress,
            scan_result,
        )
    total_tiles = int(scan_result["tiles"])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()

    target = sqlite3.connect(output_path)
    inserted = 0
    tile_format: str | None = None
    zoom_bounds: dict[int, list[int]] = {}
    try:
        _create_mbtiles(target)
        zoom_folders = _xyz_zoom_folders(tiles_root)
        last_percent = -1
        last_update = 0.0
        build_started = time.monotonic()
        if progress:
            progress(tr(f"Сборка XYZ — 0/{total_tiles:,} тайлов", f"Building XYZ — 0/{total_tiles:,} tiles"), 0)
        for zoom, zoom_folder in zoom_folders:
            limit = 1 << zoom
            zoom_count = 0
            bounds = [limit, limit, -1, -1]
            for x, y, tile_path in _iter_xyz_tiles(zoom, zoom_folder):
                blob = tile_path.read_bytes()
                try:
                    current_format = detect_tile_format(blob)
                except ValueError as error:
                    raise ValueError(tr(f"Неподдерживаемый или повреждённый тайл: {tile_path}", f"Unsupported or corrupted tile: {tile_path}")) from error
                if tile_format is None:
                    tile_format = current_format
                elif current_format != tile_format:
                    raise ValueError(tr(
                        "В папке смешаны форматы тайлов. Оставь только один: PNG, JPEG, WebP или AVIF.",
                        "The folder contains mixed tile formats. Keep only one: PNG, JPEG, WebP or AVIF.",
                    ))
                target.execute(
                    "INSERT OR REPLACE INTO tiles VALUES (?, ?, ?, ?)",
                    (zoom, x, limit - 1 - y, blob),
                )
                bounds[0] = min(bounds[0], x)
                bounds[1] = min(bounds[1], y)
                bounds[2] = max(bounds[2], x)
                bounds[3] = max(bounds[3], y)
                zoom_count += 1
                processed = inserted + zoom_count
                percent = min(100, processed * 100 // total_tiles)
                now = time.monotonic()
                if progress and (percent != last_percent or now - last_update >= 1.0):
                    elapsed = max(0.01, now - build_started)
                    speed = processed / elapsed
                    remaining = int((total_tiles - processed) / speed) if speed else 0
                    progress(
                        tr(
                            f"Сборка XYZ — {processed:,}/{total_tiles:,} · {speed:,.0f} тайл/с · осталось {remaining // 60:02d}:{remaining % 60:02d}",
                            f"Building XYZ — {processed:,}/{total_tiles:,} · {speed:,.0f} tiles/s · {remaining // 60:02d}:{remaining % 60:02d} remaining",
                        ),
                        percent,
                    )
                    last_percent = percent
                    last_update = now
            if zoom_count:
                zoom_bounds[zoom] = bounds
                inserted += zoom_count
                target.commit()
            log(tr(f"Масштаб {zoom}: добавлено {zoom_count} тайлов.", f"Zoom {zoom}: added {zoom_count} tiles."))

        if not inserted or tile_format is None:
            raise ValueError(tr("В папке не найдены тайлы со структурой z/x/y.png (или jpg/webp/avif).", "No tiles with a z/x/y.png structure were found (jpg/webp/avif are also supported)."))
        stored_tiles = int(target.execute("SELECT COUNT(*) FROM tiles").fetchone()[0])
        if stored_tiles != total_tiles:
            raise ValueError(tr(
                f"Обнаружены повторяющиеся координаты XYZ: найдено файлов {total_tiles:,}, уникальных тайлов {stored_tiles:,}. Удали дубликаты и повтори преобразование.",
                f"Duplicate XYZ coordinates detected: {total_tiles:,} files found but only {stored_tiles:,} unique tiles. Remove duplicates and try again.",
            ))
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
        if progress:
            progress(tr(f"Сборка XYZ — {inserted:,}/{total_tiles:,} тайлов", f"Building XYZ — {inserted:,}/{total_tiles:,} tiles"), 100)
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
        "source_bytes": int(scan_result["bytes"]),
    }


def _zip_member_name(info: zipfile.ZipInfo) -> str:
    """Return a normalized safe ZIP member name or reject the archive."""
    raw_name = info.filename
    if not raw_name or "\x00" in raw_name:
        raise ValueError(tr("ZIP содержит некорректное имя файла.", "The ZIP contains an invalid file name."))
    portable_name = raw_name.replace("\\", "/")
    if portable_name.startswith("/") or re.match(r"^[A-Za-z]:", portable_name):
        raise ValueError(tr("ZIP содержит абсолютный путь.", "The ZIP contains an absolute path."))
    parts = PurePosixPath(portable_name).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(tr("ZIP содержит небезопасный путь.", "The ZIP contains an unsafe path."))

    unix_mode = info.external_attr >> 16
    file_type = stat.S_IFMT(unix_mode)
    if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise ValueError(
            tr(
                "ZIP содержит ссылку или специальный файл; такой архив не поддерживается.",
                "The ZIP contains a link or special file and cannot be used.",
            )
        )
    if info.flag_bits & 0x1:
        raise ValueError(tr("Зашифрованные ZIP-файлы не поддерживаются.", "Encrypted ZIP files are not supported."))
    if not info.is_dir():
        if info.file_size > ZIP_MAX_MEMBER_BYTES:
            raise ValueError(tr("Один из файлов ZIP слишком большой.", "A ZIP member is too large."))
        if info.file_size and info.compress_size == 0:
            raise ValueError(tr("ZIP имеет подозрительное сжатие.", "The ZIP has a suspicious compression ratio."))
        if info.compress_size and info.file_size / info.compress_size > ZIP_MAX_COMPRESSION_RATIO:
            raise ValueError(tr("ZIP имеет подозрительно высокий коэффициент сжатия.", "The ZIP has a suspiciously high compression ratio."))
    return "/".join(parts)


def _xml_local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1].rsplit(":", 1)[-1].casefold()


def _parse_global_mapper_metadata(payload: bytes) -> tuple[str, str] | None:
    """Parse trusted layout facts without resolving schemas or external resources."""
    if len(payload) > GLOBAL_MAPPER_XML_LIMIT:
        raise ValueError(
            tr(
                "Файл метаданных Global Mapper превышает безопасный лимит 256 КиБ.",
                "The Global Mapper metadata file exceeds the safe 256 KiB limit.",
            )
        )
    markup_probe = payload.lower().replace(b"\x00", b"")
    if b"<!doctype" in markup_probe or b"<!entity" in markup_probe:
        raise ValueError(
            tr(
                "Небезопасные объявления DOCTYPE/ENTITY в XML запрещены.",
                "Unsafe DOCTYPE/ENTITY declarations are not allowed in XML.",
            )
        )
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as error:
        raise ValueError(
            tr("Не удалось безопасно прочитать XML Global Mapper.", "The Global Mapper XML could not be read safely.")
        ) from error

    creators: list[str] = []
    base_urls: list[str] = []
    native_projections: list[str] = []
    tile_sizes: list[str] = []
    for element in root.iter():
        local_tag = _xml_local_name(str(element.tag))
        text = (element.text or "").strip()
        if local_tag == "creator" and text:
            creators.append(text)
        elif local_tag == "baseurl" and text:
            base_urls.append(text)
        elif local_tag == "nativeproj" and text:
            native_projections.append(text)
        elif local_tag == "tilesize" and text:
            tile_sizes.append(text)
        for key, value in element.attrib.items():
            local_key = _xml_local_name(str(key))
            clean_value = str(value).strip()
            if local_key == "creator":
                creators.append(clean_value)
            elif local_key == "baseurl":
                base_urls.append(clean_value)
            elif local_key == "nativeproj":
                native_projections.append(clean_value)
            elif local_key == "tilesize":
                tile_sizes.append(clean_value)

    trusted_creator = re.compile(
        r"^global mapper(?:\s*-\s*https?://(?:www\.)?globalmapper\.com/?)?$",
        re.IGNORECASE,
    )
    if not any(trusted_creator.fullmatch(value.strip()) for value in creators):
        return None
    layout_pattern = re.compile(
        r"(?:^|[/\\])Z%z[/\\]%y[/\\]%x\.(png|jpe?g|webp|avif)(?:[?#].*)?$",
        re.IGNORECASE,
    )
    extensions = {
        match.group(1).casefold()
        for value in base_urls
        if (match := layout_pattern.search(value.strip()))
    }
    if len(extensions) != 1:
        return None
    if native_projections and not any("3857" in value for value in native_projections):
        raise ValueError(
            tr(
                "Экспорт Global Mapper использует не Web Mercator (EPSG:3857).",
                "The Global Mapper export does not use Web Mercator (EPSG:3857).",
            )
        )
    if tile_sizes and not all(value == "256" for value in tile_sizes):
        raise ValueError(
            tr(
                "Поддерживаются только тайлы Global Mapper размером 256×256.",
                "Only 256×256 Global Mapper tiles are supported.",
            )
        )
    extension = extensions.pop()
    return TILE_EXTENSIONS[extension], extension


def _read_global_mapper_zip_metadata(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo
) -> tuple[str, str] | None:
    if info.file_size > GLOBAL_MAPPER_XML_LIMIT:
        raise ValueError(
            tr(
                "Файл метаданных Global Mapper превышает безопасный лимит 256 КиБ.",
                "The Global Mapper metadata file exceeds the safe 256 KiB limit.",
            )
        )
    with archive.open(info, "r") as stream:
        payload = stream.read(GLOBAL_MAPPER_XML_LIMIT + 1)
    return _parse_global_mapper_metadata(payload)


def _read_global_mapper_file_metadata(path: Path) -> tuple[str, str] | None:
    try:
        info = path.lstat()
    except OSError as error:
        raise OSError(
            tr(
                f"Не удалось прочитать метаданные Global Mapper: {path}",
                f"Could not read Global Mapper metadata: {path}",
            )
        ) from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError(
            tr(
                "Файл метаданных Global Mapper не должен быть ссылкой или специальным файлом.",
                "Global Mapper metadata must not be a link or special file.",
            )
        )
    if info.st_size > GLOBAL_MAPPER_XML_LIMIT:
        raise ValueError(
            tr(
                "Файл метаданных Global Mapper превышает безопасный лимит 256 КиБ.",
                "The Global Mapper metadata file exceeds the safe 256 KiB limit.",
            )
        )
    with path.open("rb") as stream:
        payload = stream.read(GLOBAL_MAPPER_XML_LIMIT + 1)
    return _parse_global_mapper_metadata(payload)


def _global_mapper_tile_coordinates(
    member_name: str, metadata_prefix: str
) -> tuple[int, int, int, str] | None:
    if metadata_prefix:
        prefix = metadata_prefix.rstrip("/") + "/"
        if not member_name.startswith(prefix):
            return None
        relative_name = member_name[len(prefix):]
    else:
        relative_name = member_name
    match = re.fullmatch(
        r"(?:.+/)?Z(\d+)/(\d+)/(\d+)\.(png|jpe?g|webp|avif)",
        relative_name,
        re.IGNORECASE,
    )
    if not match:
        return None
    zoom, y, x = (int(match.group(index)) for index in range(1, 4))
    if not 0 <= zoom <= 24:
        raise ValueError(tr(f"Недопустимый масштаб в ZIP: Z{zoom}.", f"Invalid zoom level in ZIP: Z{zoom}."))
    limit = 1 << zoom
    if not (0 <= x < limit and 0 <= y < limit):
        raise ValueError(
            tr(
                f"Координаты тайла выходят за сетку Z{zoom}: x={x}, y={y}.",
                f"Tile coordinates are outside the Z{zoom} grid: x={x}, y={y}.",
            )
        )
    return zoom, x, y, match.group(4).casefold()


def _find_global_mapper_directory_metadata(
    tiles_root: Path,
) -> tuple[Path, str, str] | None:
    candidates: list[Path] = []
    for current_root, directory_names, file_names in os.walk(tiles_root, followlinks=False):
        current = Path(current_root)
        directory_names[:] = [
            name for name in directory_names if not (current / name).is_symlink()
        ]
        for file_name in file_names:
            if file_name.casefold().endswith(".gm_source_def.xml"):
                candidates.append(current / file_name)
                if len(candidates) > 32:
                    raise ValueError(
                        tr(
                            "В папке слишком много файлов метаданных Global Mapper.",
                            "The folder contains too many Global Mapper metadata files.",
                        )
                    )
    if not candidates:
        return None

    trusted: list[tuple[Path, str, str]] = []
    for path in candidates:
        detected = _read_global_mapper_file_metadata(path)
        if detected is not None:
            tile_format, declared_extension = detected
            trusted.append((path, tile_format, declared_extension))
    if len(trusted) != 1:
        raise ValueError(
            tr(
                "Папка не распознана как экспорт Global Mapper: нужен один безопасный *.gm_source_def.xml с официальным creator Global Mapper и шаблоном BaseURL Z%z/%y/%x.<формат>.",
                "The folder is not recognized as a Global Mapper export: it needs one safe *.gm_source_def.xml with an official Global Mapper creator and a Z%z/%y/%x.<format> BaseURL template.",
            )
        )
    return trusted[0]


def _iter_global_mapper_directory_tiles(
    metadata_root: Path,
) -> Iterable[tuple[int, int, int, str, Path]]:
    for zoom, zoom_folder in _xyz_zoom_folders(metadata_root):
        limit = 1 << zoom
        for y_folder in zoom_folder.iterdir():
            if y_folder.is_symlink() or not y_folder.is_dir():
                continue
            y = _xyz_index(y_folder.name)
            if y is None:
                continue
            if not 0 <= y < limit:
                raise ValueError(
                    tr(
                        f"Координата Y выходит за сетку Z{zoom}: y={y}.",
                        f"Y coordinate is outside the Z{zoom} grid: y={y}.",
                    )
                )
            for tile_path in y_folder.iterdir():
                if tile_path.is_symlink() or not tile_path.is_file():
                    continue
                extension = tile_path.suffix.lstrip(".").casefold()
                if extension not in TILE_EXTENSIONS:
                    continue
                x = _xyz_index(tile_path.stem)
                if x is None:
                    continue
                if not 0 <= x < limit:
                    raise ValueError(
                        tr(
                            f"Координата X выходит за сетку Z{zoom}: x={x}.",
                            f"X coordinate is outside the Z{zoom} grid: x={x}.",
                        )
                    )
                yield zoom, x, y, extension, tile_path


def scan_global_mapper_directory(
    tiles_root: Path,
    log: Log = print,
    progress: Progress | None = None,
    *,
    allow_absent: bool = False,
) -> dict[str, object] | None:
    """Validate and scan a read-only Global Mapper Z/Y/X directory."""
    if not tiles_root.is_dir():
        raise FileNotFoundError(
            tr(f"Не найдена папка тайлов: {tiles_root}", f"Tile folder not found: {tiles_root}")
        )
    metadata = _find_global_mapper_directory_metadata(tiles_root)
    if metadata is None:
        if allow_absent:
            return None
        raise ValueError(
            tr(
                "В папке не найден подтверждающий *.gm_source_def.xml Global Mapper.",
                "No confirming Global Mapper *.gm_source_def.xml was found in the folder.",
            )
        )
    metadata_path, tile_format, declared_extension = metadata
    metadata_root = metadata_path.parent
    started = time.monotonic()
    if progress:
        progress(tr("Проверка папки Global Mapper", "Checking Global Mapper folder"), None)
    total_tiles = 0
    total_tile_bytes = 0
    per_zoom: dict[int, int] = {}
    coordinates: set[tuple[int, int, int]] = set()
    for zoom, x, y, member_extension, tile_path in _iter_global_mapper_directory_tiles(
        metadata_root
    ):
        if TILE_EXTENSIONS[member_extension] != tile_format:
            raise ValueError(
                tr(
                    "В папке смешаны форматы тайлов или они не совпадают с XML Global Mapper.",
                    "The folder contains mixed tile formats or they do not match the Global Mapper XML.",
                )
            )
        try:
            size = tile_path.stat().st_size
        except OSError as error:
            raise OSError(
                tr(
                    f"Не удалось прочитать сведения о тайле {tile_path}.",
                    f"Could not read tile information for {tile_path}.",
                )
            ) from error
        if size > ZIP_MAX_TILE_BYTES:
            raise ValueError(
                tr("Один из тайлов слишком большой.", "A tile is too large.")
            )
        coordinate = (zoom, x, y)
        if coordinate in coordinates:
            raise ValueError(
                tr(
                    "В папке обнаружены повторяющиеся координаты тайлов.",
                    "Duplicate tile coordinates were found in the folder.",
                )
            )
        coordinates.add(coordinate)
        per_zoom[zoom] = per_zoom.get(zoom, 0) + 1
        total_tiles += 1
        total_tile_bytes += size
        if progress and total_tiles % 1000 == 0:
            progress(
                tr(
                    f"Проверка папки Global Mapper — найдено {total_tiles:,}",
                    f"Checking Global Mapper folder — {total_tiles:,} found",
                ),
                None,
            )
    if not total_tiles:
        raise ValueError(
            tr(
                "В подтверждённом экспорте Global Mapper не найдены тайлы Z<zoom>/<y>/<x>.<формат>.",
                "No Z<zoom>/<y>/<x>.<format> tiles were found in the confirmed Global Mapper export.",
            )
        )
    elapsed = max(0.01, time.monotonic() - started)
    log(
        tr(
            f"Папка Global Mapper: найдено {total_tiles:,} тайлов Z{min(per_zoom)}–Z{max(per_zoom)}, {format_bytes(total_tile_bytes)}.",
            f"Global Mapper folder: found {total_tiles:,} tiles at Z{min(per_zoom)}–Z{max(per_zoom)}, {format_bytes(total_tile_bytes)}.",
        )
    )
    if progress:
        progress(tr("Проверка папки Global Mapper", "Checking Global Mapper folder"), 100)
    return {
        "layout": "global-mapper-z-y-x",
        "source_kind": "directory",
        "metadata_path": str(metadata_path),
        "metadata_root": str(metadata_root),
        "declared_extension": declared_extension,
        "format": tile_format,
        "tiles": total_tiles,
        "bytes": total_tile_bytes,
        "zooms": per_zoom,
        "elapsed": elapsed,
    }


def build_mbtiles_from_global_mapper_directory(
    tiles_root: Path,
    output_path: Path,
    name: str,
    attribution: str,
    log: Log = print,
    progress: Progress | None = None,
    scan_result: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build TMS MBTiles from a trusted read-only Global Mapper Z/Y/X folder."""
    scan_result = scan_result or scan_global_mapper_directory(
        tiles_root, log, progress
    )
    if scan_result is None:
        raise ValueError(tr("Папка Global Mapper не распознана.", "Global Mapper folder was not recognized."))
    total_tiles = int(scan_result["tiles"])
    metadata_root = Path(str(scan_result["metadata_root"]))
    expected_format = str(scan_result["format"])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()
    target = sqlite3.connect(output_path)
    inserted = 0
    zoom_bounds: dict[int, list[int]] = {}
    started = time.monotonic()
    last_percent = -1
    last_update = 0.0
    try:
        _create_mbtiles(target)
        if progress:
            progress(
                tr(
                    f"Сборка Global Mapper — 0/{total_tiles:,} тайлов",
                    f"Building Global Mapper — 0/{total_tiles:,} tiles",
                ),
                0,
            )
        for zoom, x, y, member_extension, tile_path in _iter_global_mapper_directory_tiles(
            metadata_root
        ):
            if TILE_EXTENSIONS[member_extension] != expected_format:
                raise ValueError(
                    tr(
                        "В папке смешаны форматы тайлов или они не совпадают с XML Global Mapper.",
                        "The folder contains mixed tile formats or they do not match the Global Mapper XML.",
                    )
                )
            try:
                with tile_path.open("rb") as stream:
                    blob = stream.read(ZIP_MAX_TILE_BYTES + 1)
            except OSError as error:
                raise OSError(
                    tr(
                        f"Не удалось прочитать тайл {tile_path}.",
                        f"Could not read tile {tile_path}.",
                    )
                ) from error
            if len(blob) > ZIP_MAX_TILE_BYTES:
                raise ValueError(tr("Один из тайлов слишком большой.", "A tile is too large."))
            try:
                tile_format = detect_tile_format(blob)
            except ValueError as error:
                raise ValueError(
                    tr(
                        f"Неподдерживаемый или повреждённый тайл: {tile_path}",
                        f"Unsupported or corrupted tile: {tile_path}",
                    )
                ) from error
            if tile_format != expected_format:
                raise ValueError(
                    tr(
                        "Формат данных тайлов не совпадает с XML Global Mapper или в папке смешаны форматы.",
                        "Tile data does not match the Global Mapper XML, or the folder contains mixed formats.",
                    )
                )
            limit = 1 << zoom
            try:
                target.execute(
                    "INSERT INTO tiles VALUES (?, ?, ?, ?)",
                    (zoom, x, limit - 1 - y, blob),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError(
                    tr(
                        "В папке обнаружены повторяющиеся координаты тайлов.",
                        "Duplicate tile coordinates were found in the folder.",
                    )
                ) from error
            bounds = zoom_bounds.setdefault(zoom, [limit, limit, -1, -1])
            bounds[0] = min(bounds[0], x)
            bounds[1] = min(bounds[1], y)
            bounds[2] = max(bounds[2], x)
            bounds[3] = max(bounds[3], y)
            inserted += 1
            percent = min(100, inserted * 100 // total_tiles)
            now = time.monotonic()
            if progress and (percent != last_percent or now - last_update >= 1.0):
                elapsed = max(0.01, now - started)
                speed = inserted / elapsed
                remaining = int((total_tiles - inserted) / speed) if speed else 0
                progress(
                    tr(
                        f"Сборка Global Mapper — {inserted:,}/{total_tiles:,} · {speed:,.0f} тайл/с · осталось {remaining // 60:02d}:{remaining % 60:02d}",
                        f"Building Global Mapper — {inserted:,}/{total_tiles:,} · {speed:,.0f} tiles/s · {remaining // 60:02d}:{remaining % 60:02d} remaining",
                    ),
                    percent,
                )
                last_percent = percent
                last_update = now
            if inserted % 1000 == 0:
                target.commit()
        if inserted != total_tiles:
            raise ValueError(
                tr(
                    "Количество прочитанных тайлов папки неожиданно изменилось.",
                    "The number of folder tiles changed unexpectedly.",
                )
            )
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
            name=name or metadata_root.name,
            attribution=attribution,
            tile_format=expected_format,
            min_zoom=min_zoom,
            max_zoom=max_zoom,
            bbox=bbox,
            description="Created by THS2 Map Builder from a Global Mapper Z/Y/X directory",
            extra={"source_layout": "global-mapper-z-y-x"},
        )
        target.commit()
        if progress:
            progress(
                tr(
                    f"Сборка Global Mapper — {inserted:,}/{total_tiles:,} тайлов",
                    f"Building Global Mapper — {inserted:,}/{total_tiles:,} tiles",
                ),
                100,
            )
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
        "format": expected_format,
        "min_zoom": min_zoom,
        "max_zoom": max_zoom,
        "source_bytes": int(scan_result["bytes"]),
        "source_layout": "global-mapper-z-y-x",
        "metadata_path": str(scan_result["metadata_path"]),
    }


def scan_global_mapper_zip(
    archive_path: Path,
    log: Log = print,
    progress: Progress | None = None,
) -> dict[str, object]:
    """Validate and scan a Global Mapper Z/Y/X tile ZIP without extracting it."""
    if not archive_path.is_file():
        raise FileNotFoundError(tr(f"Не найден ZIP: {archive_path}", f"ZIP file not found: {archive_path}"))
    started = time.monotonic()
    if progress:
        progress(tr("Проверка ZIP Global Mapper", "Checking Global Mapper ZIP"), None)
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            infos = archive.infolist()
            if len(infos) > ZIP_MAX_FILES:
                raise ValueError(tr("В ZIP слишком много файлов.", "The ZIP contains too many files."))
            safe_members: list[tuple[zipfile.ZipInfo, str]] = []
            seen_names: set[str] = set()
            total_uncompressed = 0
            metadata_candidates: list[tuple[zipfile.ZipInfo, str]] = []
            for info in infos:
                member_name = _zip_member_name(info)
                folded_name = member_name.casefold()
                if folded_name in seen_names:
                    raise ValueError(tr("ZIP содержит повторяющиеся имена файлов.", "The ZIP contains duplicate file names."))
                seen_names.add(folded_name)
                total_uncompressed += info.file_size
                if total_uncompressed > ZIP_MAX_UNCOMPRESSED_BYTES:
                    raise ValueError(tr("Распакованный объём ZIP превышает безопасный лимит.", "The ZIP uncompressed size exceeds the safety limit."))
                safe_members.append((info, member_name))
                if not info.is_dir() and PurePosixPath(member_name).name.casefold().endswith(".gm_source_def.xml"):
                    metadata_candidates.append((info, member_name))

            trusted_metadata: list[tuple[str, str, str]] = []
            for info, member_name in metadata_candidates:
                detected = _read_global_mapper_zip_metadata(archive, info)
                if detected is not None:
                    tile_format, declared_extension = detected
                    trusted_metadata.append((member_name, tile_format, declared_extension))
            if len(trusted_metadata) != 1:
                raise ValueError(
                    tr(
                        "ZIP не распознан как экспорт Global Mapper: нужен один безопасный *.gm_source_def.xml с официальным creator Global Mapper и шаблоном BaseURL Z%z/%y/%x.<формат>.",
                        "The ZIP is not recognized as a Global Mapper export: it needs one safe *.gm_source_def.xml with an official Global Mapper creator and a Z%z/%y/%x.<format> BaseURL template.",
                    )
                )

            metadata_member, tile_format, declared_extension = trusted_metadata[0]
            metadata_prefix = str(PurePosixPath(metadata_member).parent)
            if metadata_prefix == ".":
                metadata_prefix = ""
            total_tiles = 0
            total_tile_bytes = 0
            per_zoom: dict[int, int] = {}
            coordinates: set[tuple[int, int, int]] = set()
            for info, member_name in safe_members:
                if info.is_dir():
                    continue
                tile = _global_mapper_tile_coordinates(member_name, metadata_prefix)
                if tile is None:
                    continue
                zoom, x, y, member_extension = tile
                if TILE_EXTENSIONS[member_extension] != tile_format:
                    raise ValueError(
                        tr(
                            "В ZIP смешаны форматы тайлов или они не совпадают с XML Global Mapper.",
                            "The ZIP contains mixed tile formats or they do not match the Global Mapper XML.",
                        )
                    )
                coordinate = (zoom, x, y)
                if info.file_size > ZIP_MAX_TILE_BYTES:
                    raise ValueError(tr("Один из тайлов ZIP слишком большой.", "A ZIP tile is too large."))
                if coordinate in coordinates:
                    raise ValueError(
                        tr(
                            "В ZIP обнаружены повторяющиеся координаты тайлов.",
                            "Duplicate tile coordinates were found in the ZIP.",
                        )
                    )
                coordinates.add(coordinate)
                zoom = coordinate[0]
                per_zoom[zoom] = per_zoom.get(zoom, 0) + 1
                total_tiles += 1
                total_tile_bytes += info.file_size
            if not total_tiles:
                raise ValueError(
                    tr(
                        "В подтверждённом экспорте Global Mapper не найдены тайлы Z<zoom>/<y>/<x>.<формат>.",
                        "No Z<zoom>/<y>/<x>.<format> tiles were found in the confirmed Global Mapper export.",
                    )
                )
    except zipfile.BadZipFile as error:
        raise ValueError(tr("ZIP повреждён или имеет неподдерживаемую структуру.", "The ZIP is damaged or has an unsupported structure.")) from error

    elapsed = max(0.01, time.monotonic() - started)
    log(
        tr(
            f"Global Mapper ZIP: найдено {total_tiles:,} тайлов Z{min(per_zoom)}–Z{max(per_zoom)}, {format_bytes(total_tile_bytes)}.",
            f"Global Mapper ZIP: found {total_tiles:,} tiles at Z{min(per_zoom)}–Z{max(per_zoom)}, {format_bytes(total_tile_bytes)}.",
        )
    )
    if progress:
        progress(tr("Проверка ZIP Global Mapper", "Checking Global Mapper ZIP"), 100)
    return {
        "layout": "global-mapper-z-y-x",
        "metadata_member": metadata_member,
        "metadata_prefix": metadata_prefix,
        "declared_extension": declared_extension,
        "format": tile_format,
        "tiles": total_tiles,
        "bytes": total_tile_bytes,
        "zooms": per_zoom,
        "elapsed": elapsed,
    }


def build_mbtiles_from_global_mapper_zip(
    archive_path: Path,
    output_path: Path,
    name: str,
    attribution: str,
    log: Log = print,
    progress: Progress | None = None,
    scan_result: dict[str, object] | None = None,
) -> dict[str, object]:
    """Stream a trusted Global Mapper Z/Y/X ZIP into a TMS MBTiles file."""
    scan_result = scan_result or scan_global_mapper_zip(archive_path, log, progress)
    total_tiles = int(scan_result["tiles"])
    metadata_prefix = str(scan_result["metadata_prefix"])
    expected_format = str(scan_result["format"])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()
    target = sqlite3.connect(output_path)
    inserted = 0
    zoom_bounds: dict[int, list[int]] = {}
    started = time.monotonic()
    last_percent = -1
    last_update = 0.0
    try:
        _create_mbtiles(target)
        if progress:
            progress(tr(f"Сборка Global Mapper — 0/{total_tiles:,} тайлов", f"Building Global Mapper — 0/{total_tiles:,} tiles"), 0)
        with zipfile.ZipFile(archive_path, "r") as archive:
            for info in archive.infolist():
                member_name = _zip_member_name(info)
                if info.is_dir():
                    continue
                tile = _global_mapper_tile_coordinates(member_name, metadata_prefix)
                if tile is None:
                    continue
                zoom, x, y, member_extension = tile
                if TILE_EXTENSIONS[member_extension] != expected_format:
                    raise ValueError(
                        tr(
                            "В ZIP смешаны форматы тайлов или они не совпадают с XML Global Mapper.",
                            "The ZIP contains mixed tile formats or they do not match the Global Mapper XML.",
                        )
                    )
                with archive.open(info, "r") as stream:
                    blob = stream.read(ZIP_MAX_TILE_BYTES + 1)
                if len(blob) != info.file_size or len(blob) > ZIP_MAX_TILE_BYTES:
                    raise ValueError(tr(f"Не удалось безопасно прочитать тайл {member_name}.", f"Tile {member_name} could not be read safely."))
                try:
                    tile_format = detect_tile_format(blob)
                except ValueError as error:
                    raise ValueError(tr(f"Неподдерживаемый или повреждённый тайл: {member_name}", f"Unsupported or corrupted tile: {member_name}")) from error
                if tile_format != expected_format:
                    raise ValueError(
                        tr(
                            "Формат данных тайлов не совпадает с XML Global Mapper или в ZIP смешаны форматы.",
                            "Tile data does not match the Global Mapper XML, or the ZIP contains mixed formats.",
                        )
                    )
                limit = 1 << zoom
                try:
                    target.execute(
                        "INSERT INTO tiles VALUES (?, ?, ?, ?)",
                        (zoom, x, limit - 1 - y, blob),
                    )
                except sqlite3.IntegrityError as error:
                    raise ValueError(tr("В ZIP обнаружены повторяющиеся координаты тайлов.", "Duplicate tile coordinates were found in the ZIP.")) from error
                bounds = zoom_bounds.setdefault(zoom, [limit, limit, -1, -1])
                bounds[0] = min(bounds[0], x)
                bounds[1] = min(bounds[1], y)
                bounds[2] = max(bounds[2], x)
                bounds[3] = max(bounds[3], y)
                inserted += 1
                percent = min(100, inserted * 100 // total_tiles)
                now = time.monotonic()
                if progress and (percent != last_percent or now - last_update >= 1.0):
                    elapsed = max(0.01, now - started)
                    speed = inserted / elapsed
                    remaining = int((total_tiles - inserted) / speed) if speed else 0
                    progress(
                        tr(
                            f"Сборка Global Mapper — {inserted:,}/{total_tiles:,} · {speed:,.0f} тайл/с · осталось {remaining // 60:02d}:{remaining % 60:02d}",
                            f"Building Global Mapper — {inserted:,}/{total_tiles:,} · {speed:,.0f} tiles/s · {remaining // 60:02d}:{remaining % 60:02d} remaining",
                        ),
                        percent,
                    )
                    last_percent = percent
                    last_update = now
                if inserted % 1000 == 0:
                    target.commit()
        if inserted != total_tiles:
            raise ValueError(tr("Количество прочитанных тайлов ZIP неожиданно изменилось.", "The number of ZIP tiles changed unexpectedly."))
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
            name=name or archive_path.stem,
            attribution=attribution,
            tile_format=expected_format,
            min_zoom=min_zoom,
            max_zoom=max_zoom,
            bbox=bbox,
            description="Created by THS2 Map Builder from a Global Mapper Z/Y/X ZIP export",
            extra={"source_layout": "global-mapper-z-y-x"},
        )
        target.commit()
        if progress:
            progress(tr(f"Сборка Global Mapper — {inserted:,}/{total_tiles:,} тайлов", f"Building Global Mapper — {inserted:,}/{total_tiles:,} tiles"), 100)
    except (zipfile.BadZipFile, RuntimeError) as error:
        target.close()
        if output_path.exists():
            output_path.unlink()
        raise ValueError(tr("ZIP повреждён или не может быть безопасно прочитан.", "The ZIP is damaged or cannot be read safely.")) from error
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
        "format": expected_format,
        "min_zoom": min_zoom,
        "max_zoom": max_zoom,
        "source_bytes": int(scan_result["bytes"]),
        "source_layout": "global-mapper-z-y-x",
        "metadata_member": str(scan_result["metadata_member"]),
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
    progress: Progress | None = None,
) -> dict:
    if not cache_root.is_dir():
        raise FileNotFoundError(tr(f"Не найдена папка кэша: {cache_root}", f"Cache folder not found: {cache_root}"))
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
                            raise ValueError(tr(
                                "В выбранной области смешаны форматы тайлов; собери карту из одного источника.",
                                "The selected area contains mixed tile formats; build the map from a single source.",
                            ))
                        tms_y = (1 << zoom) - 1 - xyz_y
                        target.execute(
                            "INSERT OR REPLACE INTO tiles VALUES (?, ?, ?, ?)",
                            (zoom, x, tms_y, blob),
                        )
                        zoom_count += 1
            inserted += zoom_count
            log(tr(f"Масштаб {zoom}: найдено {zoom_count} тайлов.", f"Zoom {zoom}: found {zoom_count} tiles."))
            if progress:
                progress(
                    tr("Чтение кэша SAS.Planet", "Reading SAS.Planet cache"),
                    (zoom - min_zoom + 1) * 100 // (max_zoom - min_zoom + 1),
                )
            target.commit()

        if inserted == 0 or tile_format is None:
            raise ValueError(tr("В выделенной области и диапазоне масштабов тайлы не найдены.", "No tiles were found in the selected area and zoom range."))
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
    raise FileNotFoundError(tr(
        "Не найден официальный PMTiles CLI. Запусти Install-PmTiles.ps1 из папки THS2 Map Builder.",
        "The official PMTiles CLI was not found. Run Install-PmTiles.ps1 from the THS2 Map Builder folder.",
    ))


def _extract_progress(text: str) -> int | None:
    percent_matches = re.findall(r"(?<!\d)(\d{1,3})\s*%", text)
    if percent_matches:
        return min(100, int(percent_matches[-1]))
    gdal_matches = re.findall(r"(?<!\d)(\d{1,3})\.\.\.", text)
    if gdal_matches:
        return min(100, int(gdal_matches[-1]))
    return None


def _safe_log(log: Log, line: str) -> None:
    try:
        log(line)
    except UnicodeEncodeError:
        # Legacy Windows consoles often use cp1251 and cannot render the
        # Unicode blocks used by the PMTiles progress bar. Logging must never
        # interrupt or orphan the conversion process.
        log(line.encode("ascii", "replace").decode("ascii"))


def run_process_streaming(
    executable: Path,
    arguments: list[str],
    log: Log = print,
    progress: Progress | None = None,
    stage: str | None = None,
    env: dict[str, str] | None = None,
    emit_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a hidden child process while streaming CR/LF progress to the GUI."""
    stage = stage or tr("Обработка", "Processing")
    command = [str(executable), *arguments]
    if progress:
        progress(stage, None)
    process = subprocess.Popen(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        env=env,
    )
    output_parts: list[str] = []
    pending = ""
    last_line = ""
    last_percent: int | None = None
    assert process.stdout is not None
    while True:
        character = process.stdout.read(1)
        if character == "" and process.poll() is not None:
            break
        if not character:
            continue
        output_parts.append(character)
        if character in "\r\n":
            clean_line = pending.strip()
            if emit_output and clean_line and clean_line != last_line:
                _safe_log(log, clean_line)
                last_line = clean_line
            pending = ""
            continue
        pending += character
        current_percent = _extract_progress(pending)
        if current_percent is not None and current_percent != last_percent:
            last_percent = current_percent
            if progress:
                progress(stage, current_percent)
    clean_line = pending.strip()
    if emit_output and clean_line and clean_line != last_line:
        _safe_log(log, clean_line)
    process.stdout.close()
    return_code = process.wait()
    if progress and return_code == 0 and last_percent != 100:
        progress(stage, 100)
    return subprocess.CompletedProcess(
        command, return_code, "".join(output_parts), ""
    )


def run_pmtiles(
    cli: Path,
    arguments: list[str],
    log: Log = print,
    check: bool = True,
    progress: Progress | None = None,
    stage: str = "PMTiles",
) -> subprocess.CompletedProcess[str]:
    result = run_process_streaming(cli, arguments, log, progress, stage)
    if check and result.returncode != 0:
        tail = result.stdout.strip()[-1000:]
        raise RuntimeError(
            tr(f"PMTiles CLI завершился с кодом {result.returncode}.", f"PMTiles CLI exited with code {result.returncode}.")
            + (f"\n{tail}" if tail else "")
        )
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
        raise FileNotFoundError(tr(
            "Для GeoTIFF и KMZ нужен GDAL. Установи OSGeo4W/QGIS или положи утилиты GDAL в папку bin\\gdal рядом с приложением. Не найдены: " + ", ".join(missing),
            "GeoTIFF and KMZ require GDAL. Install OSGeo4W/QGIS or place the GDAL tools in bin\\gdal next to the application. Missing: " + ", ".join(missing),
        ))
    return result


def run_external(
    executable: Path,
    arguments: list[str],
    log: Log = print,
    emit_output: bool = True,
    progress: Progress | None = None,
    stage: str = "GDAL",
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PATH"] = str(executable.parent) + os.pathsep + env.get("PATH", "")
    result = run_process_streaming(
        executable, arguments, log, progress, stage, env, emit_output
    )
    if result.returncode != 0:
        tail = result.stdout.strip()[-1000:]
        raise RuntimeError(
            tr(
                f"{executable.name} завершился с кодом {result.returncode}. Проверь географическую привязку исходного файла.",
                f"{executable.name} exited with code {result.returncode}. Check the source file georeferencing.",
            ) + (f"\n{tail}" if tail else "")
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
    temp_dir: Path | None = None,
    progress: Progress | None = None,
) -> dict:
    """Reproject a GeoTIFF/KMZ raster to Web Mercator and create MBTiles."""
    if not input_path.is_file():
        raise FileNotFoundError(tr(f"Не найден исходный растр: {input_path}", f"Source raster not found: {input_path}"))
    if not 0 <= min_zoom <= max_zoom <= 22:
        raise ValueError(tr("Для георастра масштабы должны быть в диапазоне 0–22.", "Raster zoom levels must be between 0 and 22."))
    tile_format = tile_format.upper()
    if tile_format not in {"PNG", "JPEG"}:
        raise ValueError(tr("Для георастра доступны тайлы PNG или JPEG.", "Raster tiles must use PNG or JPEG."))
    if not 1 <= jpeg_quality <= 100:
        raise ValueError(tr("Качество JPEG должно быть от 1 до 100.", "JPEG quality must be between 1 and 100."))

    tools = find_gdal_tools(gdal_folder)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()
    info = run_external(
        tools["gdalinfo"], ["-json", str(input_path)], log, emit_output=False,
        progress=progress, stage=tr("Проверка геопривязки", "Checking georeferencing")
    )
    try:
        info_payload = json.loads(info.stdout)
    except json.JSONDecodeError as error:
        raise ValueError(tr("GDAL не смог прочитать сведения о геопривязке.", "GDAL could not read the georeferencing information.")) from error
    if not info_payload.get("coordinateSystem") and not info_payload.get("gcps"):
        raise ValueError(tr(
            "У растра не найдена система координат. Для обычного JPG/PNG сначала нужно выполнить географическую привязку.",
            "The raster has no coordinate reference system. A regular JPG/PNG must be georeferenced first.",
        ))

    resolution = 156543.03392804097 / (1 << max_zoom)
    with tempfile.TemporaryDirectory(
        prefix="ths2-map-builder-raster-",
        dir=str(temp_dir) if temp_dir else None,
    ) as folder:
        warped = Path(folder) / "web-mercator.vrt"
        warp_arguments = [
            "-overwrite", "-of", "VRT", "-t_srs", "EPSG:3857",
            "-tr", f"{resolution:.12f}", f"{resolution:.12f}",
            "-tap", "-r", "bilinear",
        ]
        if tile_format == "PNG":
            warp_arguments.append("-dstalpha")
        warp_arguments.extend((str(input_path), str(warped)))
        log(tr(f"Перепроецирование в Web Mercator, максимальный масштаб {max_zoom}…", f"Reprojecting to Web Mercator, maximum zoom {max_zoom}…"))
        run_external(
            tools["gdalwarp"], warp_arguments, log, progress=progress,
            stage=tr("Перепроецирование", "Reprojecting")
        )

        translate_arguments = [
            "-of", "MBTILES", "-co", f"TILE_FORMAT={tile_format}",
            "-co", "TYPE=baselayer", "-co", f"NAME={name or input_path.stem}",
            "-co", "ZOOM_LEVEL_STRATEGY=LOWER",
        ]
        if tile_format == "JPEG":
            translate_arguments.extend(("-co", f"QUALITY={jpeg_quality}"))
        translate_arguments.extend((str(warped), str(output_path)))
        log(tr("Нарезка растра на тайлы…", "Creating raster tiles…"))
        run_external(
            tools["gdal_translate"], translate_arguments, log, progress=progress,
            stage=tr("Нарезка тайлов", "Creating tiles")
        )

        if min_zoom < max_zoom:
            overview_factors = [str(1 << step) for step in range(1, max_zoom - min_zoom + 1)]
            log(tr(f"Создание масштабов {min_zoom}–{max_zoom}…", f"Creating zoom levels {min_zoom}–{max_zoom}…"))
            run_external(
                tools["gdaladdo"],
                ["-r", "average", str(output_path), *overview_factors],
                log,
                progress=progress,
                stage=tr("Создание масштабов", "Creating zoom levels"),
            )

    connection = sqlite3.connect(output_path)
    try:
        zoom_row = connection.execute(
            "SELECT MIN(zoom_level), MAX(zoom_level), COUNT(*) FROM tiles"
        ).fetchone()
        if not zoom_row or zoom_row[2] == 0:
            raise ValueError(tr("GDAL не создал ни одного тайла.", "GDAL did not create any tiles."))
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
    no_deduplication: bool = False,
    temp_dir: Path | None = None,
    progress: Progress | None = None,
) -> dict:
    if not input_path.is_file():
        raise FileNotFoundError(tr(f"Не найден MBTiles: {input_path}", f"MBTiles file not found: {input_path}"))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()
    conversion_input = input_path
    temporary: tempfile.TemporaryDirectory[str] | None = None
    index_status = mbtiles_index_status(input_path)
    if name is not None or attribution is not None or not index_status["indexed"]:
        temporary = tempfile.TemporaryDirectory(
            prefix="ths2-map-builder-metadata-",
            dir=str(temp_dir) if temp_dir else None,
        )
        conversion_input = Path(temporary.name) / input_path.name
        copy_file_with_progress(input_path, conversion_input, log, progress)
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
        if progress:
            progress(tr("Индексирование MBTiles", "Indexing MBTiles"), None)
        ensure_mbtiles_index(conversion_input, log)
        if progress:
            progress(tr("Индексирование MBTiles", "Indexing MBTiles"), 100)
    else:
        log(tr("Индекс исходного MBTiles найден; дополнительная копия не требуется.", "The source MBTiles index was found; no additional copy is required."))
    try:
        arguments = ["convert"]
        if no_deduplication:
            arguments.append("--no-deduplication")
            log(tr("Быстрый режим: дедупликация тайлов отключена.", "Fast mode: tile deduplication is disabled."))
        if temp_dir:
            arguments.extend(("--tmpdir", str(temp_dir)))
        arguments.extend((str(conversion_input), str(output_path)))
        run_pmtiles(
            cli, arguments, log, progress=progress, stage=tr("Упаковка PMTiles", "Packing PMTiles")
        )
    finally:
        if temporary is not None:
            temporary.cleanup()
    run_pmtiles(
        cli, ["verify", str(output_path)], log, progress=progress,
        stage=tr("Проверка PMTiles", "Verifying PMTiles")
    )
    return read_pmtiles_header(output_path)


def read_pmtiles_header(path: Path) -> dict:
    with path.open("rb") as stream:
        header = stream.read(127)
    if len(header) != 127 or header[:7] != b"PMTiles" or header[7] != 3:
        raise ValueError(tr("Файл не является PMTiles v3.", "The file is not PMTiles v3."))
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
    no_deduplication: bool = False,
    temp_dir: Path | None = None,
    progress: Progress | None = None,
) -> dict:
    with tempfile.TemporaryDirectory(
        prefix="ths2-map-builder-", dir=str(temp_dir) if temp_dir else None
    ) as temp_folder:
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
            progress,
        )
        pmtiles_result = convert_mbtiles(
            mbtiles_path, output_path, cli, log,
            no_deduplication=no_deduplication,
            temp_dir=temp_dir,
            progress=progress,
        )
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
    no_deduplication: bool = False,
    temp_dir: Path | None = None,
    progress: Progress | None = None,
) -> dict:
    if tiles_root.is_file():
        if tiles_root.suffix.casefold() != ".zip":
            raise ValueError(
                tr(
                    "Для режима XYZ выбери папку тайлов или ZIP-экспорт Global Mapper.",
                    "For XYZ mode, select a tile folder or a Global Mapper ZIP export.",
                )
            )
        return global_mapper_zip_to_pmtiles(
            tiles_root,
            output_path,
            name,
            attribution,
            cli,
            log,
            no_deduplication,
            temp_dir,
            progress,
        )
    scan_result = scan_xyz_directory(tiles_root, log, progress)
    source_size = int(scan_result["bytes"])
    requirements = working_space_requirements(source_size, "xyz")
    temp_root = temp_dir or Path(tempfile.gettempdir())
    temp_free = shutil.disk_usage(temp_root).free
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_free = shutil.disk_usage(output_path.parent).free
    log(tr(
        f"Оценка места для XYZ: исходные тайлы {format_bytes(source_size)}, свободно во временной папке {format_bytes(temp_free)}, в папке результата {format_bytes(output_free)}.",
        f"XYZ space estimate: source tiles {format_bytes(source_size)}, temporary folder free {format_bytes(temp_free)}, output folder free {format_bytes(output_free)}.",
    ))
    minimum_temp = source_size + max(256 * 1024 ** 2, source_size // 5)
    minimum_output = max(128 * 1024 ** 2, source_size // 2)
    same_drive = temp_root.resolve().anchor.lower() == output_path.parent.resolve().anchor.lower()
    if same_drive:
        if temp_free < minimum_temp + minimum_output:
            raise OSError(tr("Недостаточно свободного места для временного MBTiles и результата XYZ.", "Not enough free space for the temporary MBTiles and XYZ output."))
    elif temp_free < minimum_temp or output_free < minimum_output:
        raise OSError(tr("Недостаточно свободного места для преобразования XYZ.", "Not enough free space for XYZ conversion."))
    if temp_free < requirements["temporary"] or output_free < requirements["output"]:
        log(tr(
            "ВНИМАНИЕ: свободного места меньше рекомендуемого; преобразование будет продолжено, так как обязательный минимум доступен.",
            "WARNING: free space is below the recommendation; conversion will continue because the required minimum is available.",
        ))
    with tempfile.TemporaryDirectory(
        prefix="ths2-map-builder-xyz-", dir=str(temp_dir) if temp_dir else None
    ) as temp_folder:
        mbtiles_path = Path(temp_folder) / "xyz-export.mbtiles"
        source_result = build_mbtiles_from_xyz(
            tiles_root, mbtiles_path, name, attribution, log, progress, scan_result
        )
        pmtiles_result = convert_mbtiles(
            mbtiles_path, output_path, cli, log,
            no_deduplication=no_deduplication,
            temp_dir=temp_dir,
            progress=progress,
        )
    result = {
        "status": "ok",
        "source_type": (
            "global-mapper-directory-read-only"
            if source_result.get("source_layout") == "global-mapper-z-y-x"
            else "xyz-directory-read-only"
        ),
        "source_layout": source_result.get("source_layout", "xyz-z-x-y"),
        "source": str(tiles_root),
        "output": str(output_path),
        "tiles": source_result,
        "pmtiles": pmtiles_result,
        "attribution_notice": attribution.strip(),
    }
    write_report(output_path.with_suffix(".report.json"), result)
    return result


def global_mapper_zip_to_pmtiles(
    archive_path: Path,
    output_path: Path,
    name: str,
    attribution: str,
    cli: Path,
    log: Log = print,
    no_deduplication: bool = False,
    temp_dir: Path | None = None,
    progress: Progress | None = None,
) -> dict:
    scan_result = scan_global_mapper_zip(archive_path, log, progress)
    source_size = int(scan_result["bytes"])
    requirements = working_space_requirements(source_size, "xyz")
    temp_root = temp_dir or Path(tempfile.gettempdir())
    temp_free = shutil.disk_usage(temp_root).free
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_free = shutil.disk_usage(output_path.parent).free
    log(
        tr(
            f"Оценка места для Global Mapper ZIP: тайлы {format_bytes(source_size)}, свободно во временной папке {format_bytes(temp_free)}, в папке результата {format_bytes(output_free)}.",
            f"Global Mapper ZIP space estimate: tiles {format_bytes(source_size)}, temporary folder free {format_bytes(temp_free)}, output folder free {format_bytes(output_free)}.",
        )
    )
    minimum_temp = source_size + max(256 * 1024 ** 2, source_size // 5)
    minimum_output = max(128 * 1024 ** 2, source_size // 2)
    same_drive = temp_root.resolve().anchor.lower() == output_path.parent.resolve().anchor.lower()
    if same_drive:
        if temp_free < minimum_temp + minimum_output:
            raise OSError(
                tr(
                    "Недостаточно свободного места для временного MBTiles и результата Global Mapper.",
                    "Not enough free space for the temporary MBTiles and Global Mapper output.",
                )
            )
    elif temp_free < minimum_temp or output_free < minimum_output:
        raise OSError(
            tr(
                "Недостаточно свободного места для преобразования Global Mapper ZIP.",
                "Not enough free space to convert the Global Mapper ZIP.",
            )
        )
    if temp_free < requirements["temporary"] or output_free < requirements["output"]:
        log(
            tr(
                "ВНИМАНИЕ: свободного места меньше рекомендуемого; преобразование будет продолжено, так как обязательный минимум доступен.",
                "WARNING: free space is below the recommendation; conversion will continue because the required minimum is available.",
            )
        )
    with tempfile.TemporaryDirectory(
        prefix="ths2-map-builder-global-mapper-",
        dir=str(temp_dir) if temp_dir else None,
    ) as temp_folder:
        mbtiles_path = Path(temp_folder) / "global-mapper-export.mbtiles"
        source_result = build_mbtiles_from_global_mapper_zip(
            archive_path,
            mbtiles_path,
            name,
            attribution,
            log,
            progress,
            scan_result,
        )
        log(
            tr(
                "Временный MBTiles построен из уже проверенных тайлов; повторная полная проверка SQLite не требуется.",
                "The temporary MBTiles was built from validated tiles; a second full SQLite check is not required.",
            )
        )
        pmtiles_result = convert_mbtiles(
            mbtiles_path,
            output_path,
            cli,
            log,
            no_deduplication=no_deduplication,
            temp_dir=temp_dir,
            progress=progress,
        )
    result = {
        "status": "ok",
        "source_type": "global-mapper-zip-read-only",
        "source_layout": "global-mapper-z-y-x",
        "source": str(archive_path),
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
    no_deduplication: bool = False,
    temp_dir: Path | None = None,
    progress: Progress | None = None,
) -> dict:
    with tempfile.TemporaryDirectory(
        prefix="ths2-map-builder-raster-", dir=str(temp_dir) if temp_dir else None
    ) as temp_folder:
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
            temp_dir,
            progress,
        )
        pmtiles_result = convert_mbtiles(
            mbtiles_path, output_path, cli, log,
            no_deduplication=no_deduplication,
            temp_dir=temp_dir,
            progress=progress,
        )
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
