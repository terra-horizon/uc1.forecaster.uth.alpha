import argparse
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from forecaster.data.collectors.sentinel2 import ImageCollection


class DummyResponse:
    def __init__(self, status_code=200, content=b"image", text=""):
        self.status_code = status_code
        self.content = content
        self.text = text


def make_collector(output_dir):
    collector = ImageCollection.__new__(ImageCollection)
    collector.bbox = [22.0, 38.0, 22.1, 38.1]
    collector.crs = "http://www.opengis.net/def/crs/OGC/1.3/CRS84"
    collector.time_from = "2026-05-31T00:00:00Z"
    collector.time_to = "2026-05-31T23:59:59Z"
    collector.upsampling = "BILINEAR"
    collector.downsampling = "BILINEAR"
    collector.tile_size = 256
    collector.output_dir = output_dir
    collector.tile_name = "global"
    collector.products = ImageCollection._default_products()
    return collector


class ImageCollectionTests(unittest.TestCase):
    def test_true_color_payload_uses_sentinel2_l2a(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            collector = make_collector(tmp_dir)
            payload = collector._build_payload("true_color")

        data_sources = payload["input"]["data"]
        self.assertEqual(data_sources[0]["type"], "sentinel-2-l2a")
        self.assertIn("B04", payload["evalscript"])
        self.assertIn("B03", payload["evalscript"])
        self.assertIn("B02", payload["evalscript"])

    def test_surface_temperature_payload_uses_sentinel3_fusion_aliases(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            collector = make_collector(tmp_dir)
            payload = collector._build_payload("surface_temperature")

        data_sources = payload["input"]["data"]
        self.assertEqual([source["id"] for source in data_sources], ["S3SLSTR", "S3OLCI"])
        self.assertEqual([source["type"] for source in data_sources], ["sentinel-3-slstr", "sentinel-3-olci"])
        self.assertIn('datasource: "S3SLSTR"', payload["evalscript"])
        self.assertIn('datasource: "S3OLCI"', payload["evalscript"])

    def test_non_200_response_returns_unavailable_metadata(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            collector = make_collector(tmp_dir)
            collector._post = lambda payload: DummyResponse(status_code=404, content=b"", text="no data")
            result = collector.fetch_one("true_color")

            self.assertEqual(result["status"], "unavailable")
            self.assertIsNone(result["path"])
            self.assertEqual(result["requested_date"], "2026-05-31")
            self.assertEqual(result["actual_date"], "N/A")
            self.assertEqual(result["collection"], "sentinel-2-l2a")
            self.assertIn("404", result["message"])
            self.assertEqual(os.listdir(tmp_dir), [])

    def test_empty_transparent_png_returns_unavailable_metadata(self):
        try:
            from PIL import Image
        except ImportError as exc:
            self.skipTest(f"Pillow unavailable: {exc}")

        with tempfile.TemporaryDirectory() as tmp_dir:
            image_path = Path(tmp_dir) / "empty.png"
            Image.new("RGBA", (1, 1), (0, 0, 0, 0)).save(image_path)
            image_bytes = image_path.read_bytes()

            collector = make_collector(tmp_dir)
            collector._post = lambda payload: DummyResponse(status_code=200, content=image_bytes)
            result = collector.fetch_one("true_color")

            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(result["actual_date"], "N/A")
            self.assertIn("No image data", result["message"])

    def test_cli_image_key_parser_defaults_and_unknown_key(self):
        try:
            from forecast import DEFAULT_IMAGE_KEYS, parse_image_keys
        except ModuleNotFoundError as exc:
            self.skipTest(f"forecast dependencies unavailable: {exc}")

        self.assertIn("true_color", DEFAULT_IMAGE_KEYS)
        self.assertIn("surface_temperature", DEFAULT_IMAGE_KEYS)
        self.assertEqual(parse_image_keys("true_color,surface_temperature"), ("true_color", "surface_temperature"))
        with self.assertRaises(argparse.ArgumentTypeError):
            parse_image_keys("true_color,not_a_product")


if __name__ == "__main__":
    unittest.main()
