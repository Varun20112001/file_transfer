import atexit
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from io import BytesIO


TEST_SAVE_DIR = tempfile.mkdtemp(prefix="sbm-download-tests-")
atexit.register(shutil.rmtree, TEST_SAVE_DIR, ignore_errors=True)
os.environ["SBM_SAVE_DIR"] = TEST_SAVE_DIR
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import sbm  # noqa: E402


class DownloadRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sbm-case-")
        self.root = self.temp.name
        sbm.SAVE_DIR = self.root
        sbm.TMP_DIR = os.path.join(self.root, ".chunks")
        sbm.DONE_FILE = os.path.join(sbm.TMP_DIR, "done.json")
        sbm.OUTGOING_DIR = os.path.join(sbm.TMP_DIR, "outgoing")
        os.makedirs(sbm.OUTGOING_DIR)
        self.client = sbm.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def write_file(self, relative, content):
        path = os.path.join(self.root, *relative.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as file:
            file.write(content)
        return path

    def test_home_page_includes_both_transfer_modes(self):
        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Send to laptop", response.data)
        self.assertIn(b"Send to phone", response.data)

    def test_browse_lists_files_and_directories_but_hides_chunks(self):
        self.write_file("A clip.mp4", b"video")
        self.write_file("Albums/one.jpg", b"photo")
        self.write_file(".chunks/internal", b"secret")

        response = self.client.get("/api/browse")

        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual([item["name"] for item in data["items"]], ["Albums", "A clip.mp4"])
        self.assertEqual(data["items"][1]["size"], 5)
        self.assertNotIn(".chunks", response.get_data(as_text=True))

    def test_browse_rejects_traversal_and_internal_chunk_paths(self):
        traversal = self.client.get("/api/browse", query_string={"path": "../outside"})
        internal = self.client.get("/api/browse", query_string={"path": ".chunks"})

        self.assertEqual(traversal.status_code, 400)
        self.assertEqual(internal.status_code, 404)

    def test_direct_download_preserves_filename_and_supports_ranges(self):
        self.write_file("A clip.mp4", b"0123456789")

        response = self.client.get("/api/download/file", query_string={"path": "A clip.mp4"},
                                   headers={"Range": "bytes=2-5"})

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.data, b"2345")
        self.assertIn('filename="A clip.mp4"', response.headers["Content-Disposition"])
        self.assertEqual(response.headers["Content-Range"], "bytes 2-5/10")
        response.close()

    def test_archive_stores_files_without_compression_and_cleans_up(self):
        self.write_file("photos/one.jpg", b"JPEG")
        self.write_file("photos/sub/two.mov", b"VIDEO")
        self.write_file("photos/sub/.chunks/hidden", b"secret")
        response = self.client.post("/api/download/archive", json={
            "base": "photos",
            "paths": ["photos/one.jpg", "photos/sub"],
        })
        self.assertEqual(response.status_code, 200)
        archive_info = response.get_json()
        download = self.client.get(archive_info["url"])
        self.assertEqual(download.status_code, 200)
        with zipfile.ZipFile(BytesIO(download.data)) as archive:
            self.assertEqual(set(archive.namelist()), {"one.jpg", "sub/", "sub/two.mov"})
            self.assertEqual(archive.read("one.jpg"), b"JPEG")
            self.assertEqual(archive.read("sub/two.mov"), b"VIDEO")
            self.assertTrue(all(info.compress_type == zipfile.ZIP_STORED for info in archive.infolist()))

        token = archive_info["url"].rsplit("/", 1)[-1]
        archive_path = os.path.join(sbm.OUTGOING_DIR, f"sbm-download-{token}.zip")
        download.close()
        self.assertFalse(os.path.exists(archive_path))

    def test_archive_rejects_out_of_root_selection(self):
        self.write_file("safe.txt", b"safe")

        response = self.client.post("/api/download/archive", json={"base": "", "paths": ["../outside"]})

        self.assertEqual(response.status_code, 400)

    def test_interrupted_archive_response_removes_temporary_zip(self):
        self.write_file("folder/file.bin", b"0123456789")
        archive_info = self.client.post("/api/download/archive", json={
            "base": "",
            "paths": ["folder"],
        }).get_json()
        token = archive_info["url"].rsplit("/", 1)[-1]
        archive_path = os.path.join(sbm.OUTGOING_DIR, f"sbm-download-{token}.zip")
        response = self.client.get(archive_info["url"], buffered=False)

        next(iter(response.response))
        response.close()

        self.assertFalse(os.path.exists(archive_path))

    def test_startup_cleanup_only_removes_sbm_outgoing_archives(self):
        stale = os.path.join(sbm.OUTGOING_DIR, "sbm-download-old.zip")
        unrelated = os.path.join(sbm.OUTGOING_DIR, "keep.txt")
        with open(stale, "wb") as file:
            file.write(b"stale")
        with open(unrelated, "wb") as file:
            file.write(b"keep")

        sbm.cleanup_outgoing_archives()

        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(unrelated))


if __name__ == "__main__":
    unittest.main()
