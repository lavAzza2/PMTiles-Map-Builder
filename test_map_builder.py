import tempfile
from pathlib import Path
import sqlite3
import unittest

from map_builder_core import (
    build_mbtiles_from_raster,
    build_mbtiles_from_cache,
    build_mbtiles_from_xyz,
    find_gdal_tools,
    find_pmtiles_cli,
    parse_hlg,
    read_pmtiles_header,
    tile_range,
    xyz_to_pmtiles,
)


class MapBuilderTests(unittest.TestCase):
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
