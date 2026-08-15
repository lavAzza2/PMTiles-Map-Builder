import tempfile
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

from i18n import (
    detect_system_language,
    get_language,
    load_language,
    load_settings,
    save_language,
    save_settings,
    set_language,
    settings_path,
    tr,
)
from map_builder_core import (
    _extract_progress,
    build_mbtiles_from_raster,
    build_mbtiles_from_cache,
    build_mbtiles_from_xyz,
    convert_mbtiles,
    ensure_mbtiles_index,
    find_gdal_tools,
    find_pmtiles_cli,
    format_bytes,
    mbtiles_index_status,
    parse_hlg,
    read_pmtiles_header,
    run_process_streaming,
    scan_xyz_directory,
    tile_range,
    xyz_to_pmtiles,
    working_space_requirements,
)


class MapBuilderTests(unittest.TestCase):
    def test_russian_and_english_localization(self):
        original = get_language()
        try:
            set_language("en")
            self.assertEqual("Ready", tr("Готов к работе", "Ready"))
            self.assertEqual("1.0 GB", format_bytes(1024 ** 3))
            set_language("ru")
            self.assertEqual("Готов к работе", tr("Готов к работе", "Ready"))
            self.assertEqual("1.0 ГБ", format_bytes(1024 ** 3))
        finally:
            set_language(original)

    def test_language_setting_is_saved_in_user_profile(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict("os.environ", {"APPDATA": folder}):
                save_settings({"mode": "xyz", "min_zoom": 8})
                save_language("en")
                self.assertEqual("en", load_language())
                self.assertEqual("xyz", load_settings()["mode"])
                self.assertEqual(8, load_settings()["min_zoom"])
                self.assertEqual(Path(folder) / "THS2 Map Builder" / "settings.json", settings_path())

    def test_system_language_detection_falls_back_to_english(self):
        with patch("i18n.os.name", "posix"):
            with patch("i18n.locale.getlocale", return_value=("ru_RU", "UTF-8")):
                self.assertEqual("ru", detect_system_language())
            with patch("i18n.locale.getlocale", return_value=("de_DE", "UTF-8")):
                self.assertEqual("en", detect_system_language())

    def test_extracts_pmtiles_and_gdal_progress(self):
        self.assertEqual(33, _extract_progress("33% | 123/456"))
        self.assertEqual(70, _extract_progress("0...10...20...70..."))
        self.assertIsNone(_extract_progress("working"))

    def test_streams_child_process_progress(self):
        events = []
        lines = []
        result = run_process_streaming(
            Path(sys.executable),
            ["-c", "import sys; sys.stdout.write('33%\\r67%\\r100%\\n'); sys.stdout.flush()"],
            lines.append,
            lambda stage, percent: events.append((stage, percent)),
            "Тест",
        )
        self.assertEqual(0, result.returncode)
        self.assertEqual(
            [("Тест", None), ("Тест", 33), ("Тест", 67), ("Тест", 100)], events
        )
        self.assertIn("100%", lines)

    def test_unicode_progress_cannot_orphan_child_process(self):
        lines = []

        def legacy_console_log(line):
            line.encode("cp1251")
            lines.append(line)

        result = run_process_streaming(
            Path(sys.executable),
            [
                "-c",
                "import sys; sys.stdout.buffer.write('progress: \\u2588 100%\\n'.encode('utf-8'))",
            ],
            legacy_console_log,
        )
        self.assertEqual(0, result.returncode)
        self.assertTrue(lines)
        self.assertNotIn("█", lines[-1])

    def test_large_map_space_recommendations_are_conservative(self):
        size = 930 * 1024 ** 2
        mbtiles = working_space_requirements(size, "mbtiles")
        raster = working_space_requirements(size, "raster")
        self.assertGreaterEqual(mbtiles["temporary"], 4 * 1024 ** 3)
        self.assertGreaterEqual(raster["temporary"], 10 * 1024 ** 3)
        self.assertGreater(raster["temporary"], mbtiles["temporary"])

    def test_parse_real_selection_shape(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "selection.hlg"
            path.write_text(
                "[HIGHLIGHTING]\nZoom=15\n"
                "PointLon_1=58.39\nPointLat_1=55.53\n"
                "PointLon_2=58.42\nPointLat_2=55.53\n"
                "PointLon_3=58.42\nPointLat_3=55.50\n"
                "PointLon_4=58.39\nPointLat_4=55.50\n",
                encoding="utf-8",
            )
            parsed = parse_hlg(path)
            self.assertEqual((58.39, 55.5, 58.42, 55.53), parsed["bbox"])

    def test_known_halilovo_tile_range(self):
        self.assertEqual(
            (5424, 5426, 2568, 2572),
            tile_range((58.359375, 55.47885346, 58.44726563, 55.55349546), 13),
        )

    def test_builds_tms_mbtiles_from_readonly_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            selection = root / "selection.hlg"
            selection.write_text(
                "[HIGHLIGHTING]\nZoom=15\n"
                "PointLon_1=0\nPointLat_1=1\n"
                "PointLon_2=1\nPointLat_2=1\n"
                "PointLon_3=1\nPointLat_3=0\n",
                encoding="utf-8",
            )
            min_x, _, min_y, _ = tile_range((0, 0, 1, 1), 8)
            database = (
                root
                / "cache"
                / "z9"
                / str(min_x // 1024)
                / str(min_y // 1024)
                / f"{min_x // 256}.{min_y // 256}.sqlitedb"
            )
            database.parent.mkdir(parents=True)
            source = sqlite3.connect(database)
            source.execute(
                "CREATE TABLE t (x INTEGER,y INTEGER,v INTEGER,c TEXT,s INTEGER,h INTEGER,d INTEGER,b BLOB)"
            )
            source.execute(
                "INSERT INTO t VALUES (?, ?, 0, NULL, 0, 0, 0, ?)",
                (min_x, min_y, b"\xff\xd8\xfftest"),
            )
            source.commit()
            source.close()
            output = root / "result.mbtiles"
            result = build_mbtiles_from_cache(
                root / "cache", selection, output, 8, 8, "Test", "Example", lambda _: None
            )
            self.assertEqual(1, result["inserted"])
            target = sqlite3.connect(output)
            row = target.execute(
                "SELECT zoom_level,tile_column,tile_row FROM tiles"
            ).fetchone()
            target.close()
            self.assertEqual((8, min_x, (1 << 8) - 1 - min_y), row)

    def test_builds_mbtiles_from_xyz_folder(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tile = root / "tiles" / "8" / "128" / "127.png"
            tile.parent.mkdir(parents=True)
            tile.write_bytes(b"\x89PNG\r\n\x1a\nmock tile")
            output = root / "xyz.mbtiles"
            result = build_mbtiles_from_xyz(
                root / "tiles", output, "XYZ test", "Example", lambda _: None
            )
            self.assertEqual(1, result["inserted"])
            self.assertEqual((8, 8), (result["min_zoom"], result["max_zoom"]))
            connection = sqlite3.connect(output)
            row = connection.execute(
                "SELECT zoom_level,tile_column,tile_row FROM tiles"
            ).fetchone()
            metadata = dict(connection.execute("SELECT name,value FROM metadata"))
            connection.close()
            self.assertEqual((8, 128, 128), row)
            self.assertEqual("png", metadata["format"])
            self.assertEqual("Example", metadata["attribution"])

    def test_xyz_prefixed_names_and_tile_progress(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for y in (126, 127):
                tile = root / "tiles" / "z8" / "x128" / f"y{y}.png"
                tile.parent.mkdir(parents=True, exist_ok=True)
                tile.write_bytes(b"\x89PNG\r\n\x1a\nmock tile")

            progress_events = []
            scan = scan_xyz_directory(
                root / "tiles",
                lambda _: None,
                lambda stage, percent: progress_events.append((stage, percent)),
            )
            self.assertEqual(2, scan["tiles"])
            self.assertIn(None, [percent for _, percent in progress_events])

            progress_events.clear()
            output = root / "prefixed.mbtiles"
            result = build_mbtiles_from_xyz(
                root / "tiles",
                output,
                "Prefixed XYZ",
                "Example",
                lambda _: None,
                lambda stage, percent: progress_events.append((stage, percent)),
                scan,
            )
            self.assertEqual(2, result["inserted"])
            build_events = [event for event in progress_events if event[0].startswith("Сборка XYZ")]
            self.assertEqual(0, build_events[0][1])
            self.assertEqual(100, build_events[-1][1])
            self.assertTrue(any("2/2" in stage for stage, _ in build_events))

    def test_rejects_mixed_xyz_tile_formats(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first = root / "tiles" / "1" / "0" / "0.png"
            second = root / "tiles" / "1" / "0" / "1.jpg"
            first.parent.mkdir(parents=True)
            first.write_bytes(b"\x89PNG\r\n\x1a\nmock")
            second.write_bytes(b"\xff\xd8\xffmock")
            output = root / "mixed.mbtiles"
            with self.assertRaisesRegex(ValueError, "смешаны форматы"):
                build_mbtiles_from_xyz(
                    root / "tiles", output, "Mixed", "", lambda _: None
                )
            self.assertFalse(output.exists())

    def test_xyz_pipeline_creates_verified_pmtiles_when_cli_available(self):
        try:
            cli = find_pmtiles_cli()
        except FileNotFoundError:
            self.skipTest("PMTiles CLI is not installed")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tile = root / "tiles" / "8" / "128" / "127.png"
            tile.parent.mkdir(parents=True)
            tile.write_bytes(b"\x89PNG\r\n\x1a\nmock tile")
            output = root / "map.pmtiles"
            result = xyz_to_pmtiles(
                root / "tiles", output, "XYZ pipeline", "Example", cli, lambda _: None
            )
            self.assertTrue(output.is_file())
            self.assertEqual(3, result["pmtiles"]["version"])
            self.assertEqual(1, result["pmtiles"]["addressed_tiles"])

    def test_large_mbtiles_working_copy_gets_index_and_fast_mode(self):
        try:
            cli = find_pmtiles_cli()
        except FileNotFoundError:
            self.skipTest("PMTiles CLI is not installed")
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source_path = root / "without-index.mbtiles"
            connection = sqlite3.connect(source_path)
            connection.executescript(
                """
                CREATE TABLE metadata (name TEXT, value TEXT);
                CREATE TABLE tiles (
                    zoom_level INTEGER, tile_column INTEGER,
                    tile_row INTEGER, tile_data BLOB
                );
                """
            )
            connection.executemany(
                "INSERT INTO metadata VALUES (?, ?)",
                (("name", "No index"), ("format", "png"), ("scheme", "tms")),
            )
            connection.execute(
                "INSERT INTO tiles VALUES (8, 128, 128, ?)",
                (b"\x89PNG\r\n\x1a\nmock tile",),
            )
            connection.commit()
            connection.close()
            self.assertFalse(mbtiles_index_status(source_path)["indexed"])

            working_copy = root / "working.mbtiles"
            working_copy.write_bytes(source_path.read_bytes())
            created = ensure_mbtiles_index(working_copy, lambda _: None)
            self.assertTrue(created["created"])
            self.assertTrue(mbtiles_index_status(working_copy)["indexed"])

            temp_root = root / "temp"
            temp_root.mkdir()
            output = root / "fast.pmtiles"
            log_lines = []
            progress_events = []
            result = convert_mbtiles(
                source_path,
                output,
                cli,
                log_lines.append,
                "Large test",
                "Example",
                no_deduplication=True,
                temp_dir=temp_root,
                progress=lambda stage, percent: progress_events.append((stage, percent)),
            )
            self.assertEqual(3, result["version"])
            self.assertFalse(mbtiles_index_status(source_path)["indexed"])
            self.assertTrue(any("дедупликация" in line for line in log_lines))
            self.assertIn(("Копирование MBTiles", 100), progress_events)

    def test_converts_real_geotiff_with_gdal_when_available(self):
        try:
            tools = find_gdal_tools()
        except FileNotFoundError:
            self.skipTest("GDAL is not installed")
        gdal_create = tools["gdal_translate"].parent / "gdal_create.exe"
        if not gdal_create.is_file():
            self.skipTest("gdal_create is not installed")
        import subprocess

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.tif"
            subprocess.run(
                [
                    str(gdal_create), "-of", "GTiff", "-outsize", "32", "32",
                    "-bands", "3", "-burn", "120", "-a_srs", "EPSG:4326",
                    "-a_ullr", "0", "1", "1", "0", str(source),
                ],
                check=True,
                capture_output=True,
            )
            output = root / "raster.mbtiles"
            result = build_mbtiles_from_raster(
                source, output, 7, 8, "Raster test", "Example", "JPEG", 80,
                log=lambda _: None,
            )
            self.assertGreater(result["inserted"], 0)
            self.assertEqual("jpg", result["format"])
            connection = sqlite3.connect(output)
            metadata = dict(connection.execute("SELECT name,value FROM metadata"))
            zooms = connection.execute(
                "SELECT MIN(zoom_level),MAX(zoom_level) FROM tiles"
            ).fetchone()
            connection.close()
            self.assertEqual("Raster test", metadata["name"])
            self.assertEqual("Example", metadata["attribution"])
            self.assertEqual((7, 8), zooms)

            kmz = root / "source.kmz"
            subprocess.run(
                [str(tools["gdal_translate"]), "-of", "KMLSUPEROVERLAY", str(source), str(kmz)],
                check=True,
                capture_output=True,
            )
            kmz_output = root / "kmz.mbtiles"
            kmz_result = build_mbtiles_from_raster(
                kmz, kmz_output, 7, 8, "KMZ test", "Example", "PNG", 85,
                log=lambda _: None,
            )
            self.assertGreater(kmz_result["inserted"], 0)
            self.assertEqual("png", kmz_result["format"])


if __name__ == "__main__":
    unittest.main()
