import json
import unittest
from pathlib import Path

from scripts.map_server import INDEX_HTML, MapData


V83_ROOT = Path(__file__).resolve().parents[1] / "data" / "v83"


@unittest.skipUnless(
    (V83_ROOT / "Map.wz").is_file() and (V83_ROOT / "String.wz").is_file(),
    "data/v83 fixture is not installed",
)
class LegacyMapServerJsonExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = MapData(V83_ROOT, region="GMS", override_dir=None)
        cls.data.open()

    @classmethod
    def tearDownClass(cls):
        cls.data.close()

    def test_map_image_json_matches_canonical_img_schema(self):
        raw = self.data.map_image_json("100000000")
        self.assertIsNotNone(raw)
        payload = json.loads(raw)
        self.assertEqual(payload["name"], "100000000.img")
        self.assertEqual(payload["type"], "Image")
        info = next(child for child in payload["children"] if child["name"] == "info")
        self.assertEqual(info["type"], "SubProperty")
        map_mark = next(child for child in info["children"] if child["name"] == "mapMark")
        self.assertEqual(map_mark, {
            "name": "mapMark", "type": "String", "value": "Henesys",
        })

    def test_viewer_exposes_export_button_and_download_route(self):
        self.assertIn('id="exportJson"', INDEX_HTML)
        self.assertIn("/export/map/", INDEX_HTML)
        self.assertIn(".img.json", INDEX_HTML)


if __name__ == "__main__":
    unittest.main()
