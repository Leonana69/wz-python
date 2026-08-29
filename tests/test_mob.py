import io
import unittest
from pathlib import Path

from PIL import Image

from wzpy.mob import MobRenderer
from wzpy.wz_file import WzFile


V83_ROOT = Path(__file__).resolve().parents[1] / "data" / "v83"


@unittest.skipUnless((V83_ROOT / "Mob.wz").is_file(), "data/v83 fixture is not installed")
class LegacyV83MobRendererTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sources = {
            name: WzFile.open(str(V83_ROOT / f"{name}.wz"), region="GMS")
            for name in ("Mob", "String", "Item", "Character")
        }
        cls.renderer = MobRenderer(
            cls.sources["Mob"],
            string_source=cls.sources["String"],
            item_source=cls.sources["Item"],
            character_source=cls.sources["Character"],
            region="GMS",
        )

    @classmethod
    def tearDownClass(cls):
        for source in cls.sources.values():
            source.close()

    def test_search_and_snail_description(self):
        self.assertEqual(
            self.renderer.search("Orange Mushroom", limit=1)[0],
            {"id": "1210102", "name": "Orange Mushroom"},
        )
        info = self.renderer.describe("100100")
        self.assertEqual(info["name"], "Snail")
        self.assertEqual(info["stats"]["level"], 1)
        self.assertEqual(info["stats"]["hp"], 8)
        self.assertIn("move", {action["name"] for action in info["actions"]})

    def test_monster_book_rewards_have_v83_names(self):
        info = self.renderer.describe("100100")
        self.assertTrue(info["hasDropData"])
        self.assertEqual(info["dropSource"], "String.wz/MonsterBook.img")
        drops = {item["id"]: item["name"] for item in info["drops"]}
        self.assertEqual(drops["1002067"], "Green Headband")
        self.assertEqual(drops["2000000"], "Red Potion")

    def test_animation_is_multi_frame_apng(self):
        payload = self.renderer.animation_png("100100", "move")
        self.assertTrue(payload.startswith(b"\x89PNG\r\n\x1a\n"))
        image = Image.open(io.BytesIO(payload))
        self.assertEqual(image.n_frames, 5)
        self.assertEqual(image.info.get("loop"), 0)

    def test_equipment_and_item_icons_decode(self):
        for item_id in ("1002067", "2000000"):
            payload = self.renderer.item_icon_png(item_id)
            self.assertIsNotNone(payload)
            self.assertTrue(payload.startswith(b"\x89PNG\r\n\x1a\n"))


@unittest.skipUnless((V83_ROOT / "Mob.wz").is_file(), "data/v83 fixture is not installed")
class LegacyV83MobBrowserApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from server.app import create_app

        cls.app = create_app(
            str(V83_ROOT / "Mob.wz"), region="GMS", mob_mode=True,
        )
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        renderer = cls.app.config.get("MOB_RENDERER")
        if renderer is not None:
            renderer.close()
        source = cls.app.config.get("WZ")
        if source is not None:
            source.close()

    def test_browser_search_info_animation_and_icon_endpoints(self):
        self.assertEqual(self.client.get("/").location, "/mob")
        self.assertEqual(self.client.get("/mob").status_code, 200)

        search = self.client.get("/api/mob/search?q=Orange%20Mushroom&limit=1")
        self.assertEqual(search.status_code, 200)
        self.assertEqual(search.get_json()[0]["id"], "1210102")

        info = self.client.get("/api/mob/100100")
        self.assertEqual(info.status_code, 200)
        self.assertEqual(info.get_json()["name"], "Snail")

        animation = self.client.get("/api/mob/100100/animation.png?action=move")
        self.assertEqual(animation.status_code, 200)
        self.assertEqual(animation.mimetype, "image/png")
        self.assertIn(b"acTL", animation.data)
        self.assertIn("mob;dur=", animation.headers["Server-Timing"])

        icon = self.client.get("/api/mob/item/1002067/icon.png")
        self.assertEqual(icon.status_code, 200)
        self.assertEqual(icon.mimetype, "image/png")

    def test_bad_mob_and_action_are_reported(self):
        self.assertEqual(self.client.get("/api/mob/not-an-id").status_code, 400)
        self.assertEqual(
            self.client.get("/api/mob/100100/animation.png?action=missing").status_code,
            404,
        )


if __name__ == "__main__":
    unittest.main()
