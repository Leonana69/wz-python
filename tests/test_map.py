import math
import unittest
from pathlib import Path

from PIL import Image

from wzpy.map import MapBounds, MapRenderer
from wzpy.wz_file import WzFile


V83_ROOT = Path(__file__).resolve().parents[1] / "data" / "v83"


@unittest.skipUnless((V83_ROOT / "Map.wz").is_file(), "data/v83 fixture is not installed")
class LegacyV83MapRendererTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sources = {
            name: WzFile.open(str(V83_ROOT / f"{name}.wz"), region="GMS")
            for name in ("Map", "String", "Mob", "Npc", "Reactor")
        }
        cls.renderer = MapRenderer(
            cls.sources["Map"],
            string_source=cls.sources["String"],
            mob_source=cls.sources["Mob"],
            npc_source=cls.sources["Npc"],
            reactor_source=cls.sources["Reactor"],
            region="GMS",
        )

    @classmethod
    def tearDownClass(cls):
        for source in cls.sources.values():
            source.close()

    def test_search_and_linked_map_resolution(self):
        self.assertEqual(self.renderer.search("Henesys", limit=1)[0]["id"], "100000000")
        linked = self.renderer.describe("100020123")
        self.assertTrue(linked["linked"])
        self.assertEqual(linked["sourceId"], "100020100")

    def test_describe_exposes_harepacker_board_categories(self):
        info = self.renderer.describe("100000001")
        self.assertEqual(info["counts"]["tiles"], 84)
        self.assertEqual(info["counts"]["objects"], 21)
        self.assertEqual(info["counts"]["npcs"], 1)
        self.assertEqual(info["counts"]["portals"], 2)
        self.assertEqual(len(info["layers"]), 8)

    def test_indirect_background_sets_are_indexed_and_rendered(self):
        backgrounds = self.sources["Map"].root.get("Back")
        self.assertIsNotNone(backgrounds)
        self.assertIsNotNone(backgrounds.get("grassySoil.img"))

        image = self.renderer.compose(
            "100000000", scale=0.125, layers=[], life=False, reactors=False,
        )
        self.assertIsNotNone(image.getbbox())
        self.assertGreater(image.getchannel("A").getextrema()[1], 0)

    def test_convex_tile_metadata_does_not_shift_canvas_pixels(self):
        tile_image = self.sources["Map"].root.get("Tile/woodMarble.img")
        tile_set = tile_image.parse()
        edge = tile_set.get("enH0/0")

        self.assertFalse(tile_image.truncated)
        self.assertEqual(tile_image.parse_warnings, [])
        self.assertEqual(edge.value, (90, 33, 1))
        self.assertEqual(edge.get("foothold").value, [(0, -26), (90, -26)])
        self.assertEqual(
            self.renderer._pixels(edge, self.sources["Map"]).size,
            (90, 33),
        )

    def test_fractional_scale_keeps_adjacent_tile_edges_joined(self):
        output = Image.new("RGBA", (45, 5), (0, 0, 0, 0))
        tile = Image.new("RGBA", (90, 20), (255, 255, 255, 255))
        bounds = MapBounds(0, 0, 180, 20)

        MapRenderer._paste_scaled(output, bounds, 0.25, tile, 0, 0, 255)
        MapRenderer._paste_scaled(output, bounds, 0.25, tile, 90, 0, 255)

        self.assertEqual(output.getchannel("A").getextrema(), (255, 255))

    def test_compose_and_minimap_are_real_png_content(self):
        info = self.renderer.describe("100000001")
        scale = 0.125
        image = self.renderer.compose(
            "100000001", scale=scale,
            portals=True, footholds=True, ropes=True,
        )
        self.assertEqual(image.size, (
            math.ceil(info["bounds"]["width"] * scale),
            math.ceil(info["bounds"]["height"] * scale),
        ))
        self.assertIsNotNone(image.getbbox())
        self.assertGreater(image.getchannel("A").getextrema()[1], 0)
        self.assertTrue(self.renderer.minimap_png("100000001").startswith(b"\x89PNG\r\n\x1a\n"))


@unittest.skipUnless((V83_ROOT / "Map.wz").is_file(), "data/v83 fixture is not installed")
class LegacyV83MapBuilderApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from server.app import create_app

        cls.app = create_app(
            str(V83_ROOT / "Map.wz"), region="GMS", map_mode=True,
        )
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        renderer = cls.app.config.get("MAP_RENDERER")
        if renderer is not None:
            renderer.close()
        source = cls.app.config.get("WZ")
        if source is not None:
            source.close()

    def test_builder_and_render_endpoints(self):
        self.assertEqual(self.client.get("/").location, "/map")
        self.assertEqual(self.client.get("/map").status_code, 200)
        search = self.client.get("/api/map/search?q=Henesys&limit=1")
        self.assertEqual(search.status_code, 200)
        self.assertEqual(search.get_json()[0]["id"], "100000000")

        render = self.client.get(
            "/api/map/100000001/render.png?scale=0.125&portals=1&footholds=1"
        )
        self.assertEqual(render.status_code, 200)
        self.assertEqual(render.mimetype, "image/png")
        self.assertTrue(render.data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertIn("map;dur=", render.headers["Server-Timing"])

    def test_render_rejects_unbounded_output_scale(self):
        response = self.client.get("/api/map/100000001/render.png?scale=5")
        self.assertEqual(response.status_code, 400)
        self.assertIn("scale", response.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
