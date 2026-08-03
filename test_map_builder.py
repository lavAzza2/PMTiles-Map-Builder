import tempfile
from pathlib import Path
import sqlite3
import unittest

from map_builder_core import (
    build_mbtiles_from_cache,
    parse_hlg,
    read_pmtiles_header,
    tile_range,
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


if __name__ == "__main__":
    unittest.main()
