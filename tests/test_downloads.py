import atexit
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

TEST_SAVE_DIR = tempfile.mkdtemp(prefix="sbm-drive-tests-")
atexit.register(shutil.rmtree, TEST_SAVE_DIR, ignore_errors=True)
os.environ["SBM_SAVE_DIR"] = TEST_SAVE_DIR
os.environ["SBM_DATABASE_URL"] = "sqlite:///sbm-tests.sqlite3"
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import sbm  # noqa: E402
def cleanup_test_database():
    with sbm.app.app_context():
        sbm.db.engine.dispose()
    for suffix in ("", "-journal", "-wal", "-shm"):
        try:
            os.remove(os.path.join(sbm.app.instance_path, "sbm-tests.sqlite3" + suffix))
        except OSError:
            pass
atexit.register(cleanup_test_database)


class DriveBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sbm-case-")
        self.root = self.temp.name
        sbm.SAVE_DIR = self.root
        sbm.TMP_DIR = os.path.join(self.root, ".chunks")
        sbm.DONE_FILE = os.path.join(sbm.TMP_DIR, "done.json")
        sbm.OUTGOING_DIR = os.path.join(sbm.TMP_DIR, "outgoing")
        os.makedirs(sbm.OUTGOING_DIR, exist_ok=True)
        sbm.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        with sbm.app.app_context():
            sbm.db.drop_all()
            sbm.db.create_all()
            self.admin = sbm.User(username="admin", password_hash=sbm.generate_password_hash("test-password-123"), role="admin")
            sbm.db.session.add(self.admin)
            sbm.db.session.commit()
            self.admin_id = self.admin.id
        self.drive = os.path.splitdrive(self.root)[0]
        self.drive = self.drive if self.drive else "C:"
        self.drive_patch = patch.object(sbm, "list_fixed_drives", return_value=[self.root])
        self.drive_patch.start()
        self.client = sbm.app.test_client()
        self.client.post("/login", data={"username": "admin", "password": "test-password-123"})
        sbm.audit_buffer.flush(sbm.app)

    def tearDown(self):
        sbm.audit_buffer.flush(sbm.app)
        with sbm.app.app_context():
            sbm.db.session.remove()
        self.drive_patch.stop()
        self.temp.cleanup()

    def write_file(self, relative, content):
        path = os.path.join(self.root, *relative.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as file:
            file.write(content)
        return path

    def test_home_and_upload_api_remain_available_without_login(self):
        self.client.post("/logout")
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Send to phone", response.data)
        response = self.client.post("/init", json={"id": "abcdef12", "total": 1, "size": 4, "name": "x.bin"})
        self.assertEqual(response.status_code, 200)
        response = self.client.post("/chunk?id=abcdef12&index=0", data=b"data", content_type="application/octet-stream")
        self.assertEqual(response.status_code, 200)
        response = self.client.post("/complete", json={"id": "abcdef12"})
        self.assertEqual(response.status_code, 200)
        with open(os.path.join(self.root, "x.bin"), "rb") as uploaded:
            self.assertEqual(uploaded.read(), b"data")
        protected = self.client.get("/api/drives")
        self.assertEqual(protected.status_code, 401)

    def test_windows_drive_discovery_filters_non_fixed_volumes(self):
        self.drive_patch.stop()
        types = {"C:\\": 3, "D:\\": 2, "E:\\": 4}
        kernel = SimpleNamespace(
            GetLogicalDrives=lambda: (1 << 2) | (1 << 3) | (1 << 4),
            GetDriveTypeW=lambda root: types[root],
        )
        with patch.object(sbm.os, "name", "nt"), patch.object(sbm.ctypes, "windll", SimpleNamespace(kernel32=kernel), create=True):
            self.assertEqual(sbm.list_fixed_drives(), ["C:\\"])
        self.drive_patch = patch.object(sbm, "list_fixed_drives", return_value=[self.root])
        self.drive_patch.start()

    def test_drive_discovery_and_browse_metadata_hide_internal_state(self):
        self.write_file("A clip.mp4", b"video")
        self.write_file("Albums/one.jpg", b"photo")
        self.write_file(".chunks/internal", b"secret")
        drives = self.client.get("/api/drives").get_json()["drives"]
        self.assertEqual(drives[0]["id"], self.drive[:2])
        response = self.client.get("/api/browse", query_string={"drive": self.drive[:2], "path": ""})
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual([item["name"] for item in data["items"]], ["Albums", "A clip.mp4"])
        self.assertEqual(data["items"][1]["type"], "video")
        image = self.client.get("/api/browse", query_string={"drive": self.drive[:2], "path": "Albums"}).get_json()["items"][0]
        self.assertEqual(image["type"], "image")
        self.assertIn("thumbnail", image)

    def test_traversal_and_reparse_points_are_rejected(self):
        outside = tempfile.NamedTemporaryFile(delete=False)
        outside.write(b"private")
        outside.close()
        try:
            response = self.client.get("/api/browse", query_string={"drive": self.drive[:2], "path": "../outside"})
            self.assertEqual(response.status_code, 400)
            link = os.path.join(self.root, "escape")
            try:
                os.symlink(os.path.dirname(outside.name), link, target_is_directory=True)
            except (OSError, NotImplementedError):
                # Simulate Windows reparse-point detection when this account lacks symlink privilege.
                with patch.object(sbm, "_is_reparse_point", side_effect=lambda path: os.path.basename(path) == "escape"):
                    self.assert_reparse_path_rejected()
            else:
                self.assert_reparse_path_rejected()
        finally:
            os.unlink(outside.name)

    def assert_reparse_path_rejected(self):
        query = {"drive": self.drive[:2], "path": "escape"}
        for endpoint in ("/api/browse", "/api/thumbnail", "/api/preview", "/api/image-preview"):
            self.assertEqual(self.client.get(endpoint, query_string=query).status_code, 403)
        self.assertEqual(self.client.post("/api/download/ticket", json=query).status_code, 403)
        self.assertEqual(self.client.post("/api/download/archive", json={"drive": self.drive[:2], "base": "", "paths": ["escape"]}).status_code, 403)

    def test_thumbnail_and_full_image_preview(self):
        from PIL import Image
        path = os.path.join(self.root, "photo.jpg")
        Image.new("RGB", (640, 480), "blue").save(path)
        thumbnail_response = self.client.get("/api/thumbnail", query_string={"drive": self.drive[:2], "path": "photo.jpg"})
        self.assertEqual(thumbnail_response.status_code, 200)
        with Image.open(BytesIO(thumbnail_response.data)) as image:
            self.assertLessEqual(max(image.size), 256)
        thumbnail_response.close()
        preview = self.client.get("/api/preview", query_string={"drive": self.drive[:2], "path": "photo.jpg"}, headers={"Range": "bytes=0-9"})
        self.assertEqual(preview.status_code, 206)
        self.assertEqual(len(preview.data), 10)
        preview.close()
        heic_path = os.path.join(self.root, "phone-photo.heic")
        Image.new("RGB", (320, 240), "green").save(heic_path, format="HEIF")
        heic_thumb = self.client.get("/api/thumbnail", query_string={"drive": self.drive[:2], "path": "phone-photo.heic"})
        self.assertEqual(heic_thumb.status_code, 200)
        heic_thumb.close()

    def test_favorites_list_cross_drive_metadata_stale_removal_and_user_scope(self):
        self.write_file("album/cover.jpg", b"not-decoded-until-preview")
        self.write_file("clip.mp4", b"video")
        self.client.post("/api/favorites", json={"drive": self.drive[:2], "path": "album", "favorite": True})
        self.client.post("/api/favorites", json={"drive": self.drive[:2], "path": "album/cover.jpg", "favorite": True})
        self.client.post("/api/favorites", json={"drive": self.drive[:2], "path": "clip.mp4", "favorite": True})
        with sbm.app.app_context():
            sbm.db.session.add(sbm.Favorite(user_id=self.admin_id, drive=self.drive[:2], path="missing/old.jpg", item_type="file"))
            sbm.db.session.commit()
        listed = self.client.get("/api/favorites")
        self.assertEqual(listed.status_code, 200)
        entries = listed.get_json()["items"]
        by_path = {entry["path"]: entry for entry in entries}
        self.assertTrue(by_path["album"]["available"])
        self.assertEqual(by_path["album"]["type"], "directory")
        self.assertEqual(by_path["album/cover.jpg"]["type"], "image")
        self.assertIn("thumbnail", by_path["album/cover.jpg"])
        self.assertEqual(by_path["clip.mp4"]["type"], "video")
        self.assertFalse(by_path["missing/old.jpg"]["available"])
        removed = self.client.post("/api/favorites", json={"drive": self.drive[:2], "path": "missing/old.jpg", "favorite": False})
        self.assertEqual(removed.status_code, 200)
        self.assertNotIn("missing/old.jpg", {entry["path"] for entry in self.client.get("/api/favorites").get_json()["items"]})

        self.client.post("/logout")
        with sbm.app.app_context():
            other = sbm.User(username="other", password_hash=sbm.generate_password_hash("other-password-123"), role="user")
            sbm.db.session.add(other)
            sbm.db.session.commit()
        response = self.client.get("/api/favorites")
        self.assertEqual(response.status_code, 401)
        self.client.post("/login", data={"username": "other", "password": "other-password-123"})
        self.assertEqual(self.client.get("/api/favorites").get_json()["items"], [])

    def test_optimized_image_preview_is_cached_bounded_and_preserves_alpha(self):
        from PIL import Image
        large_path = os.path.join(self.root, "large.jpg")
        Image.new("RGB", (3000, 1500), "navy").save(large_path, quality=95)
        params = {"drive": self.drive[:2], "path": "large.jpg"}
        audit = self.client.post("/api/audit/preview", json=params)
        self.assertEqual(audit.status_code, 200)
        preview_url = audit.get_json()["url"]
        self.assertIn("image-preview", preview_url)
        first = self.client.get(preview_url)
        self.assertEqual(first.status_code, 200)
        with Image.open(BytesIO(first.data)) as rendition:
            self.assertLessEqual(max(rendition.size), 2048)
            self.assertEqual(rendition.format, "JPEG")
        first_bytes = first.data
        first.close()
        second = self.client.get(preview_url)
        self.assertEqual(second.data, first_bytes)
        second.close()

        Image.new("RGB", (3000, 1500), "red").save(large_path, quality=95)
        refreshed = self.client.get(preview_url)
        self.assertNotEqual(refreshed.data, first_bytes)
        with Image.open(BytesIO(refreshed.data)) as rendition:
            self.assertGreater(rendition.getpixel((0, 0))[0], 180)
        refreshed.close()

        oriented_path = os.path.join(self.root, "oriented.jpg")
        oriented = Image.new("RGB", (40, 80), "orange")
        exif = oriented.getexif()
        exif[274] = 6
        oriented.save(oriented_path, exif=exif)
        orientation_response = self.client.get("/api/image-preview", query_string={"drive": self.drive[:2], "path": "oriented.jpg"})
        with Image.open(BytesIO(orientation_response.data)) as rendition:
            self.assertEqual(rendition.size, (80, 40))
        orientation_response.close()

        alpha_path = os.path.join(self.root, "alpha.png")
        Image.new("RGBA", (80, 40), (20, 80, 140, 90)).save(alpha_path)
        alpha = self.client.get("/api/image-preview", query_string={"drive": self.drive[:2], "path": "alpha.png"})
        self.assertEqual(alpha.status_code, 200)
        self.assertEqual(alpha.mimetype, "image/png")
        with Image.open(BytesIO(alpha.data)) as rendition:
            self.assertEqual(rendition.mode, "RGBA")
            self.assertEqual(rendition.getpixel((0, 0))[3], 90)
        alpha.close()

    def test_delete_file_is_confirmed_by_api_and_audited_with_actor(self):
        self.write_file("delete me.txt", b"private file")
        response = self.client.post("/api/delete", json={"drive": self.drive[:2], "path": "delete me.txt"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["deleted_by"], "admin")
        self.assertFalse(os.path.exists(os.path.join(self.root, "delete me.txt")))
        with sbm.app.app_context():
            event = sbm.AuditEvent.query.filter_by(action="delete", path="delete me.txt").one()
            self.assertEqual(event.user.username, "admin")
            self.assertEqual(event.drive, self.drive[:2].upper())
        audit = self.client.get("/admin/audit?action=delete")
        self.assertEqual(audit.status_code, 200)
        self.assertIn(b"delete me.txt", audit.data)
        self.assertIn(b"admin", audit.data)

    def test_delete_rejects_folders_and_records_failed_file_removal(self):
        os.makedirs(os.path.join(self.root, "folder"), exist_ok=True)
        folder = self.client.post("/api/delete", json={"drive": self.drive[:2], "path": "folder"})
        self.assertEqual(folder.status_code, 400)
        self.write_file("locked.bin", b"in use")
        with patch("sbm.os.remove", side_effect=PermissionError("locked")):
            failed = self.client.post("/api/delete", json={"drive": self.drive[:2], "path": "locked.bin"})
        self.assertEqual(failed.status_code, 409)
        self.assertTrue(os.path.exists(os.path.join(self.root, "locked.bin")))
        with sbm.app.app_context():
            event = sbm.AuditEvent.query.filter_by(action="delete_failed", path="locked.bin").one()
            self.assertEqual(event.user.username, "admin")

    def test_thumbnail_cleanup_removes_expired_app_cache_and_worker_repeats(self):
        import time
        import hashlib
        now = time.time()
        expired_name = hashlib.sha256(b"expired-cache-test").hexdigest() + ".jpg"
        partial_name = hashlib.sha256(b"partial-cache-test").hexdigest() + ".jpg." + ("a" * 32) + ".tmp"
        fresh_name = hashlib.sha256(b"fresh-cache-test").hexdigest() + ".png"
        arbitrary_name = "leave-this-file-alone.txt"
        expired = os.path.join(sbm.thumbnail_dir, expired_name)
        partial = os.path.join(sbm.thumbnail_dir, partial_name)
        fresh = os.path.join(sbm.thumbnail_dir, fresh_name)
        arbitrary = os.path.join(sbm.thumbnail_dir, arbitrary_name)
        try:
            for path in (expired, partial, fresh, arbitrary):
                with open(path, "wb") as cached:
                    cached.write(b"cache")
            os.utime(expired, (now - 10 * 86400, now - 10 * 86400))
            os.utime(partial, (now - 2 * 86400, now - 2 * 86400))
            cleaned = sbm.cleanup_thumbnail_cache(max_age_seconds=7 * 86400, now=now)
            self.assertEqual(cleaned["files"], 2)
            self.assertFalse(os.path.exists(expired))
            self.assertFalse(os.path.exists(partial))
            self.assertTrue(os.path.exists(fresh))
            self.assertTrue(os.path.exists(arbitrary))

            worker = sbm.start_thumbnail_cleanup_worker(interval_seconds=0.02, max_age_seconds=1)
            try:
                os.utime(fresh, (time.time() - 2, time.time() - 2))
                deadline = time.time() + 1
                while os.path.exists(fresh) and time.time() < deadline:
                    time.sleep(0.01)
                self.assertFalse(os.path.exists(fresh))
            finally:
                worker.shutdown()
        finally:
            for path in (expired, partial, fresh, arbitrary):
                try:
                    os.remove(path)
                except OSError:
                    pass

    def test_file_ticket_supports_ranges_and_logs_once(self):
        self.write_file("A clip.mp4", b"0123456789")
        create = self.client.post("/api/download/ticket", json={"drive": self.drive[:2], "path": "A clip.mp4"})
        self.assertEqual(create.status_code, 200)
        url = create.get_json()["url"]
        for _ in range(2):
            response = self.client.get(url, headers={"Range": "bytes=2-5"})
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.data, b"2345")
            response.close()
        self.assertEqual(sbm.audit_buffer.pending_count(), 1)

    def test_archive_keeps_paths_without_compression_and_audits_each_file(self):
        self.write_file("photos/one.jpg", b"JPEG")
        self.write_file("photos/sub/two.mov", b"VIDEO")
        self.write_file("photos/sub/.chunks/hidden", b"secret")
        response = self.client.post("/api/download/archive", json={
            "drive": self.drive[:2], "base": "photos", "paths": ["photos/one.jpg", "photos/sub"],
        })
        self.assertEqual(response.status_code, 200)
        archive_info = response.get_json()
        download = self.client.get(archive_info["url"])
        self.assertEqual(download.status_code, 200)
        with zipfile.ZipFile(BytesIO(download.data)) as archive:
            self.assertEqual(set(archive.namelist()), {"one.jpg", "sub/", "sub/two.mov"})
            self.assertEqual(archive.read("one.jpg"), b"JPEG")
            self.assertTrue(all(info.compress_type == zipfile.ZIP_STORED for info in archive.infolist()))
        self.assertEqual(sbm.audit_buffer.pending_count(), 2)

    def test_preview_action_is_audited_once_and_audit_page_flushes(self):
        self.write_file("still.png", b"image bytes")
        response = self.client.post("/api/audit/preview", json={"drive": self.drive[:2], "path": "still.png"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(sbm.audit_buffer.pending_count(), 1)
        page = self.client.get("/admin/audit")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(sbm.audit_buffer.pending_count(), 0)
        self.assertIn(b"still.png", page.data)

    def test_audit_buffer_bulk_flushes_at_twenty(self):
        for index in range(20):
            sbm.audit_buffer.add(sbm.app, self.admin_id, "download", self.drive[:2], f"f{index}.jpg")
        self.assertEqual(sbm.audit_buffer.pending_count(), 0)
        with sbm.app.app_context():
            self.assertEqual(sbm.AuditEvent.query.count(), 20)

    def test_audit_worker_flushes_on_interval(self):
        worker = sbm.start_audit_flush_worker(sbm.app, interval_seconds=0.02)
        try:
            sbm.audit_buffer.add(sbm.app, self.admin_id, "preview", self.drive[:2], "timer.jpg")
            import time
            deadline = time.time() + 1
            while time.time() < deadline and sbm.audit_buffer.pending_count():
                time.sleep(0.01)
            self.assertEqual(sbm.audit_buffer.pending_count(), 0)
            with sbm.app.app_context():
                self.assertEqual(sbm.AuditEvent.query.filter_by(path="timer.jpg").count(), 1)
        finally:
            worker.shutdown()

    def test_audit_worker_flushes_on_shutdown(self):
        worker = sbm.start_audit_flush_worker(sbm.app, interval_seconds=60)
        sbm.audit_buffer.add(sbm.app, self.admin_id, "download", self.drive[:2], "shutdown.mov")
        worker.shutdown()
        self.assertEqual(sbm.audit_buffer.pending_count(), 0)
        with sbm.app.app_context():
            self.assertEqual(sbm.AuditEvent.query.filter_by(path="shutdown.mov").count(), 1)

    def test_archive_traversal_rejected(self):
        self.write_file("safe.txt", b"safe")
        response = self.client.post("/api/download/archive", json={"drive": self.drive[:2], "base": "", "paths": ["../outside"]})
        self.assertEqual(response.status_code, 400)


class AccountFlowTests(unittest.TestCase):
    def setUp(self):
        sbm.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        with sbm.app.app_context():
            sbm.db.drop_all()
            sbm.db.create_all()
        self.client = sbm.app.test_client()

    def tearDown(self):
        with sbm.app.app_context():
            sbm.db.session.remove()

    def test_first_admin_setup_password_hash_admin_user_management_and_logout(self):
        token = sbm.get_setup_token()
        response = self.client.post("/setup", data={"token": token, "username": "rootadmin", "password": "admin-password-123"})
        self.assertEqual(response.status_code, 302)
        self.assertIn(b'data-can-delete="true"', self.client.get("/").data)
        with sbm.app.app_context():
            admin = sbm.User.query.filter_by(username="rootadmin").one()
            self.assertNotEqual(admin.password_hash, "admin-password-123")
            self.assertTrue(sbm.check_password_hash(admin.password_hash, "admin-password-123"))
        page = self.client.get("/admin/users")
        self.assertEqual(page.status_code, 200)
        created = self.client.post("/admin/users", data={"username": "phoneuser", "password": "phone-password-123"})
        self.assertEqual(created.status_code, 200)
        with sbm.app.app_context():
            user = sbm.User.query.filter_by(username="phoneuser").one()
            user_id = user.id
        self.client.post("/logout")
        self.client.post("/login", data={"username": "phoneuser", "password": "phone-password-123"})
        self.assertIn(b'data-can-delete="false"', self.client.get("/").data)
        self.assertEqual(self.client.get("/api/drives").status_code, 200)
        self.assertEqual(self.client.post("/api/delete", json={"drive": "C:", "path": "not-authorized.txt"}).status_code, 403)
        self.assertEqual(self.client.get("/admin/users").status_code, 403)
        self.client.post("/logout")
        self.client.post("/login", data={"username": "rootadmin", "password": "admin-password-123"})
        self.client.post("/admin/users", data={"action": "disable", "user_id": str(user_id)})
        self.client.post("/logout")
        response = self.client.post("/login", data={"username": "phoneuser", "password": "phone-password-123"})
        self.assertIn(response.status_code, (200, 302))
        self.assertEqual(self.client.get("/api/drives").status_code, 401)


if __name__ == "__main__":
    unittest.main()
