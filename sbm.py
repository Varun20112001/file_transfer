# receive_chunked.py  (pip install flask)
import atexit, hashlib, hmac, mimetypes, os, re, json, shutil, socket, uuid, threading, zipfile, time, secrets, ctypes
import ipaddress
from datetime import datetime, timezone
from io import BytesIO
from urllib.parse import urlparse
from flask import Flask, Response, abort, request, jsonify, render_template, send_file, stream_with_context, url_for, redirect, flash
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from flask_migrate import upgrade as migrate_upgrade
from flask_wtf import CSRFProtect
from PIL import Image, ImageOps, UnidentifiedImageError
from pillow_heif import register_heif_opener
from werkzeug.utils import secure_filename
from werkzeug.exceptions import HTTPException
from werkzeug.security import generate_password_hash, check_password_hash
from database import db, migrate, User, AuditEvent, Favorite, audit_buffer, start_audit_flush_worker

register_heif_opener()

SAVE_DIR = os.getenv("SBM_SAVE_DIR", "D:\\Documents\\transfer")
TMP_DIR = os.path.join(SAVE_DIR, ".chunks")
DONE_FILE = os.path.join(TMP_DIR, "done.json")
OUTGOING_DIR = os.path.join(TMP_DIR, "outgoing")
MAX_ZIP_FILES = 100_000            # safety limits for zip extraction
MAX_ZIP_BYTES = 50 * 1024 ** 3     # 50 GB uncompressed
OUTGOING_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
os.makedirs(OUTGOING_DIR, exist_ok=True)


def cleanup_outgoing_archives():
    """Remove only archives owned by SBM's outbound-download feature."""
    try:
        entries = list(os.scandir(OUTGOING_DIR))
    except OSError:
        return
    for entry in entries:
        if entry.name.startswith("sbm-download-") and entry.name.endswith((".zip", ".tmp")):
            try:
                if entry.is_file(follow_symlinks=False):
                    os.remove(entry.path)
            except OSError:
                pass


cleanup_outgoing_archives()
os.makedirs(TMP_DIR, exist_ok=True)


app = Flask(__name__,
             template_folder=os.path.join('templates'),
             static_folder=os.path.join('static'))
app.instance_path = os.path.abspath(os.getenv("SBM_INSTANCE_DIR", app.instance_path))
os.makedirs(app.instance_path, exist_ok=True)

def _load_secret_key():
    configured = os.getenv("SBM_SECRET_KEY")
    if configured:
        return configured
    key_path = os.path.join(app.instance_path, "secret.key")
    try:
        with open(key_path, "rb") as key_file:
            return key_file.read()
    except FileNotFoundError:
        key = secrets.token_bytes(32)
        with open(key_path, "wb") as key_file:
            key_file.write(key)
        return key

database_url = os.getenv("SBM_DATABASE_URL", "sqlite:///sbm.sqlite3")
if database_url.startswith("postgres://"):
    database_url = "postgresql+psycopg://" + database_url[len("postgres://"):]
elif database_url.startswith("postgresql://"):
    database_url = "postgresql+psycopg://" + database_url[len("postgresql://"):]
app.config.update(
    SECRET_KEY=_load_secret_key(),
    SQLALCHEMY_DATABASE_URI=database_url,
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=False,  # SBM remains HTTP on a trusted local connection.
)
db.init_app(app)
migrate.init_app(app, db)
csrf = CSRFProtect(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Sign in to browse and download files."
thumbnail_dir = os.path.join(app.instance_path, "thumbnails")
os.makedirs(thumbnail_dir, exist_ok=True)
THUMBNAIL_CACHE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
THUMBNAIL_CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60
download_tickets = {}
download_ticket_lock = threading.Lock()
setup_lock = threading.Lock()
ID_RE = re.compile(r"^[0-9a-f]{6,32}$")
# Allow unrelated uploads to assemble concurrently while preventing duplicate
# /complete requests for the same upload from racing each other.
complete_locks = tuple(threading.Lock() for _ in range(64))
destination_lock = threading.Lock()  # protects unique-name selection and final replace
done_lock = threading.Lock()       # protects done.json


@login_manager.user_loader
def load_user(user_id):
    try:
        return db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None


@login_manager.unauthorized_handler
def unauthorized():
    if request.path.startswith("/api/"):
        return jsonify(error="authentication required", login=url_for("login", next=request.url)), 401
    return redirect(url_for("login", next=request.url))


def get_setup_token(create=True):
    token_path = os.path.join(app.instance_path, "setup.token")
    if os.path.exists(token_path):
        with open(token_path, "r", encoding="utf-8") as token_file:
            return token_file.read().strip()
    if not create:
        return None
    token = secrets.token_urlsafe(24)
    with open(token_path, "w", encoding="utf-8") as token_file:
        token_file.write(token)
    return token


def ensure_setup_token():
    if not User.query.filter_by(role="admin").first():
        token = get_setup_token()
        app.logger.warning("SBM first-admin setup token: %s", token)
        print(f"SBM first-admin setup token: {token}")
    else:
        try:
            os.remove(os.path.join(app.instance_path, "setup.token"))
        except OSError:
            pass


def is_admin():
    return current_user.is_authenticated and current_user.role == "admin"


def admin_required(view):
    from functools import wraps
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if not is_admin():
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def safe_next_url(value):
    if not value:
        return url_for("index")
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or "\\" in value or any(ord(char) < 32 for char in value) or not value.startswith("/") or value.startswith("//"):
        return url_for("index")
    return value


def resolve_save_path(relative_path=""):
    """Resolve a browser-supplied path strictly inside SAVE_DIR."""
    if not isinstance(relative_path, str):
        abort(400, description="path must be a string")

    relative_path = relative_path.replace("\\", "/")
    if "\x00" in relative_path:
        abort(400, description="invalid path")
    if relative_path.startswith("/") or re.match(r"^[A-Za-z]:", relative_path):
        abort(400, description="absolute paths are not allowed")
    parts = relative_path.split("/") if relative_path else []
    if any(part in ("", ".", "..") for part in parts):
        abort(400, description="invalid path")
    if any(":" in part for part in parts):
        abort(400, description="invalid path")
    if any(part.casefold() == ".chunks" for part in parts):
        abort(404)

    root = os.path.realpath(SAVE_DIR)
    candidate = root
    for part in parts:
        candidate = os.path.join(candidate, part)
        # Do not follow file or directory symlinks from a client request.
        if os.path.islink(candidate):
            abort(403, description="symbolic links are not available")

    resolved = os.path.realpath(candidate)
    try:
        inside_root = os.path.normcase(os.path.commonpath([root, resolved])) == os.path.normcase(root)
    except ValueError:
        inside_root = False
    if not inside_root:
        abort(403, description="path is outside the transfer folder")

    resolved_rel = os.path.relpath(resolved, root)
    if resolved_rel != "." and any(part.casefold() == ".chunks" for part in resolved_rel.split(os.sep)):
        abort(404)
    if not os.path.exists(resolved):
        abort(404)
    return resolved


def relative_save_path(path):
    return os.path.relpath(path, os.path.realpath(SAVE_DIR)).replace(os.sep, "/")


def collect_archive_entries(base_path, selected_paths):
    """Return deduplicated (source, archive name, is_dir, size) ZIP entries."""
    base_real = os.path.realpath(base_path)
    found = {}

    def add_file(source):
        archive_name = os.path.relpath(source, base_real).replace(os.sep, "/")
        if archive_name.startswith("../") or archive_name == "..":
            abort(403, description="selection is outside the current folder")
        found.setdefault(archive_name, (source, archive_name, False, os.path.getsize(source)))

    def walk_directory(directory):
        try:
            with os.scandir(directory) as scan:
                children = sorted(scan, key=lambda child: child.name.casefold())
        except OSError:
            abort(403, description="cannot read selected folder")
        if not children:
            archive_name = os.path.relpath(directory, base_real).replace(os.sep, "/")
            if archive_name not in (".", ""):
                found.setdefault(archive_name + "/", (directory, archive_name + "/", True, 0))
        for child in children:
            if child.name.casefold() == ".chunks" or child.is_symlink():
                continue
            child_path = os.path.join(directory, child.name)
            resolve_save_path(relative_save_path(child_path))
            try:
                if child.is_dir(follow_symlinks=False):
                    archive_name = os.path.relpath(child_path, base_real).replace(os.sep, "/")
                    found.setdefault(archive_name + "/", (child_path, archive_name + "/", True, 0))
                    walk_directory(child_path)
                elif child.is_file(follow_symlinks=False):
                    add_file(child_path)
            except OSError:
                continue

    for selected in selected_paths:
        selected_path = resolve_save_path(selected)
        try:
            is_within_base = os.path.normcase(os.path.commonpath([base_real, selected_path])) == os.path.normcase(base_real)
        except ValueError:
            is_within_base = False
        if not is_within_base or selected_path == base_real:
            abort(403, description="selection is outside the current folder")
        if os.path.isdir(selected_path):
            archive_name = os.path.relpath(selected_path, base_real).replace(os.sep, "/")
            found.setdefault(archive_name + "/", (selected_path, archive_name + "/", True, 0))
            walk_directory(selected_path)
        elif os.path.isfile(selected_path):
            add_file(selected_path)
        else:
            abort(400, description="selected item is not a file or folder")

    entries = list(found.values())
    if not entries:
        abort(400, description="selection contains no downloadable files")
    if len(entries) > MAX_ZIP_FILES or sum(entry[3] for entry in entries) > MAX_ZIP_BYTES:
        abort(413, description="selection exceeds ZIP limits")
    return entries



def read_done():
    try:
        with open(DONE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_done(data):
    tmp = DONE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, DONE_FILE)


def mark_done(upload_id, rel_path):
    with done_lock:
        data = read_done()
        data[upload_id] = rel_path
        write_done(data)


def clear_done(upload_id):
    with done_lock:
        data = read_done()
        if data.pop(upload_id, None) is not None:
            write_done(data)


def safe_parts(rel):
    """Turn 'My Folder/sub/a b.jpg' into sanitised path parts. Drops '', '.', '..' and separators."""
    parts = [secure_filename(p) for p in re.split(r"[\\/]+", rel or "")]
    parts = [p for p in parts if p]
    return parts or ["file"]


def unique_path(directory, name):
    path = os.path.join(directory, name)
    if os.path.exists(path):
        name = f"{uuid.uuid4().hex[:8]}_{name}"
        path = os.path.join(directory, name)
    return name, path


def extract_zip(zip_path, dest_dir, zip_stem):
    """Extract a zip next to itself. Returns (root_folder_name, file_count).

    If the archive has a single top-level folder (what iOS Compress produces for a
    folder), it is extracted as-is. Otherwise a folder named after the zip is created.
    Every member path is sanitised, so entries cannot escape the destination.
    """
    with zipfile.ZipFile(zip_path) as zf:
        entries = []
        for info in zf.infolist():
            if info.is_dir():
                continue
            raw = info.filename.replace("\\", "/")
            if raw.startswith("__MACOSX/") or os.path.basename(raw) in (".DS_Store", "Thumbs.db"):
                continue
            entries.append((info, safe_parts(raw)))
        if not entries:
            raise ValueError("zip is empty")
        if len(entries) > MAX_ZIP_FILES or sum(i.file_size for i, _ in entries) > MAX_ZIP_BYTES:
            raise ValueError("zip exceeds size limits")

        single_top = len({p[0] for _, p in entries}) == 1 and all(len(p) > 1 for _, p in entries)
        if single_top:
            root = entries[0][1][0]
            base_dir = dest_dir
        else:
            root = secure_filename(zip_stem) or "extracted"
            base_dir = os.path.join(dest_dir, root)

        written_total = 0
        for info, parts in entries:
            target_dir = os.path.join(base_dir, *parts[:-1])
            os.makedirs(target_dir, exist_ok=True)
            _, target = unique_path(target_dir, parts[-1])
            with zf.open(info) as src, open(target, "wb") as dst:
                while True:
                    block = src.read(1024 * 1024)
                    if not block:
                        break
                    written_total += len(block)
                    if written_total > MAX_ZIP_BYTES:
                        raise ValueError("zip exceeds size limits")
                    dst.write(block)
        return root, len(entries)


def upload_dir(upload_id):
    return os.path.join(TMP_DIR, upload_id)


def load_meta(upload_id):
    if not ID_RE.match(upload_id or ""):
        return None
    path = os.path.join(upload_dir(upload_id), "meta.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def received_chunks(upload_id, total):
    d = upload_dir(upload_id)
    return [i for i in range(total) if os.path.exists(os.path.join(d, f"part_{i}"))]


@app.route("/")
def index():
    from flask_wtf.csrf import generate_csrf
    return render_template("index.html", csrf_token_value=generate_csrf(),
                           can_delete=current_user.is_authenticated and current_user.role == "admin")


def connection_ipv4_addresses():
    addresses = set()
    try:
        import psutil
        interface_states = psutil.net_if_stats()
        for name, interface_addresses in psutil.net_if_addrs().items():
            state = interface_states.get(name)
            if state is not None and not state.isup:
                continue
            for interface_address in interface_addresses:
                if interface_address.family != socket.AF_INET:
                    continue
                address = ipaddress.ip_address(interface_address.address)
                if not address.is_loopback and not address.is_link_local:
                    addresses.add(str(address))
    except ImportError:
        try:
            for result in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                address = ipaddress.ip_address(result[4][0])
                if not address.is_loopback and not address.is_link_local:
                    addresses.add(str(address))
        except OSError:
            app.logger.exception("Could not enumerate laptop IPv4 addresses")
    except OSError:
        app.logger.exception("Could not enumerate laptop IPv4 addresses")
    return sorted(addresses) or ["127.0.0.1"]


@app.route("/connect")
def connection_qr_page():
    addresses = connection_ipv4_addresses()
    return render_template("connect.html", addresses=addresses, port=int(os.getenv("SBM_PORT", "8000")))


@app.route("/connect/qr.svg")
def connection_qr_image():
    address = request.args.get("ip", "")
    if address not in connection_ipv4_addresses():
        abort(400, description="Choose one of this laptop's detected IPv4 addresses.")
    import qrcode
    from qrcode.image.svg import SvgPathImage

    target = f"http://{address}:{int(os.getenv('SBM_PORT', '8000'))}/"
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=4)
    qr.add_data(target)
    image = qr.make_image(image_factory=SvgPathImage)
    output = BytesIO()
    image.save(output)
    output.seek(0)
    return send_file(output, mimetype="image/svg+xml", max_age=0)


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv", ".3gp"}


def list_fixed_drives():
    """Return Windows fixed drive roots only; mapped/network/removable drives are excluded."""
    if os.name != "nt":
        return []
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    roots = []
    for index in range(26):
        if mask & (1 << index):
            root = f"{chr(65 + index)}:\\"
            if ctypes.windll.kernel32.GetDriveTypeW(root) == 3:  # DRIVE_FIXED
                roots.append(root)
    return roots


def _is_reparse_point(path):
    try:
        if os.path.islink(path):
            return True
        junction_check = getattr(os.path, "isjunction", None)
        if junction_check and junction_check(path):
            return True
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
        import stat
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except OSError:
        return True


def resolve_drive_path(drive, relative_path=""):
    if not isinstance(drive, str) or not re.fullmatch(r"[A-Za-z]:", drive):
        abort(400, description="invalid drive")
    root = next((item for item in list_fixed_drives() if item[:2].casefold() == drive.casefold()), None)
    if not root:
        abort(404, description="drive is unavailable")
    if not isinstance(relative_path, str):
        abort(400, description="path must be a string")
    normalized = relative_path.replace("\\", "/")
    if "\x00" in normalized or normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        abort(400, description="absolute paths are not allowed")
    parts = normalized.split("/") if normalized else []
    if any(part in ("", ".", "..") or ":" in part for part in parts):
        abort(400, description="invalid path")
    root_real = os.path.realpath(root)
    candidate = root_real
    for part in parts:
        candidate = os.path.join(candidate, part)
        if _is_reparse_point(candidate):
            abort(403, description="symbolic links and junctions are not available")
    resolved = os.path.realpath(candidate)
    try:
        if os.path.normcase(os.path.commonpath([root_real, resolved])) != os.path.normcase(root_real):
            abort(403, description="path is outside the selected drive")
    except ValueError:
        abort(403, description="path is outside the selected drive")

    internal_roots = [os.path.realpath(TMP_DIR), os.path.realpath(app.instance_path)]
    for internal in internal_roots:
        try:
            if os.path.normcase(os.path.commonpath([internal, resolved])) == os.path.normcase(internal):
                abort(404)
        except ValueError:
            pass
    # Keep the upload implementation's private state hidden if it is on the selected drive.
    if os.path.normcase(os.path.splitdrive(resolved)[0]) == os.path.normcase(os.path.splitdrive(SAVE_DIR)[0]):
        try:
            if os.path.normcase(os.path.commonpath([os.path.realpath(SAVE_DIR), resolved])) == os.path.normcase(os.path.realpath(SAVE_DIR)):
                rel = os.path.relpath(resolved, os.path.realpath(SAVE_DIR))
                if rel != "." and any(part.casefold() == ".chunks" for part in rel.split(os.sep)):
                    abort(404)
        except ValueError:
            pass
    if not os.path.exists(resolved):
        abort(404)
    return resolved


def drive_relative_path(root, path):
    return os.path.relpath(path, root).replace(os.sep, "/") if os.path.normcase(path) != os.path.normcase(root) else ""


def _path_contains(parent, child):
    """Return whether child is parent or a descendant of parent."""
    try:
        return os.path.normcase(os.path.commonpath([parent, child])) == os.path.normcase(parent)
    except ValueError:
        return False


def _validate_recursive_delete(path, drive_root):
    """Preflight a directory before recursive removal; never cross reparse points or app data."""
    target = os.path.realpath(path)
    root = os.path.realpath(drive_root)
    if os.path.normcase(target) == os.path.normcase(root):
        abort(400, description="a drive root cannot be deleted")

    protected = [os.path.realpath(TMP_DIR), os.path.realpath(app.instance_path)]
    if os.path.normcase(os.path.splitdrive(target)[0]) == os.path.normcase(os.path.splitdrive(SAVE_DIR)[0]):
        protected.append(os.path.realpath(os.path.join(SAVE_DIR, ".chunks")))
    for protected_path in protected:
        if _path_contains(target, protected_path) or _path_contains(protected_path, target):
            abort(403, description="folders containing SBM internal data cannot be deleted")

    # A selected folder can contain junctions even though the selected path itself is safe.
    # Reject them during preflight so deletion never traverses into another location.
    for current, dirs, files in os.walk(path, topdown=True, followlinks=False):
        for name in dirs + files:
            child = os.path.join(current, name)
            if _is_reparse_point(child):
                abort(403, description="folders containing symbolic links or junctions cannot be deleted")
            resolved = os.path.realpath(child)
            if not _path_contains(root, resolved):
                abort(403, description="folder contains a path outside the selected drive")
        dirs[:] = [name for name in dirs if not _is_reparse_point(os.path.join(current, name))]


def _queue_audit(action, drive, path):
    audit_buffer.add(app, current_user.id, action, drive.upper(), path.replace("\\", "/"))


def cleanup_thumbnail_cache(max_age_seconds=THUMBNAIL_CACHE_MAX_AGE_SECONDS, now=None):
    """Prune old cache files owned by the thumbnail/preview endpoints."""
    now = time.time() if now is None else now
    removed_files = removed_bytes = 0
    try:
        entries = list(os.scandir(thumbnail_dir))
    except OSError:
        app.logger.exception("Could not scan the thumbnail cache")
        return {"files": 0, "bytes": 0}
    for entry in entries:
        cache_file = re.fullmatch(r"[0-9a-f]{64}\.(?:jpg|png)", entry.name)
        partial_file = re.fullmatch(r"[0-9a-f]{64}\.(?:jpg|png)\.[0-9a-f]{32}\.tmp", entry.name)
        if not cache_file and not partial_file:
            continue
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
            info = entry.stat(follow_symlinks=False)
            expiry = max_age_seconds if cache_file else min(max_age_seconds, 24 * 60 * 60)
            if now - info.st_mtime < expiry:
                continue
            os.remove(entry.path)
            removed_files += 1
            removed_bytes += info.st_size
        except OSError:
            app.logger.debug("Could not remove expired thumbnail cache file %s", entry.path, exc_info=True)
    if removed_files:
        app.logger.info("Removed %s expired thumbnail cache files (%s bytes)", removed_files, removed_bytes)
    return {"files": removed_files, "bytes": removed_bytes}


class ThumbnailCleanupWorker:
    def __init__(self, interval_seconds, max_age_seconds):
        self.interval_seconds = interval_seconds
        self.max_age_seconds = max_age_seconds
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="sbm-thumbnail-cleaner", daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop_event.wait(self.interval_seconds):
            cleanup_thumbnail_cache(self.max_age_seconds)

    def shutdown(self):
        self.stop_event.set()
        self.thread.join(timeout=2)


def start_thumbnail_cleanup_worker(interval_seconds=THUMBNAIL_CLEANUP_INTERVAL_SECONDS,
                                   max_age_seconds=THUMBNAIL_CACHE_MAX_AGE_SECONDS):
    cleanup_thumbnail_cache(max_age_seconds)
    worker = ThumbnailCleanupWorker(interval_seconds, max_age_seconds)
    atexit.register(worker.shutdown)
    return worker


def _ticket(user_id, kind, drive, path, filename=None):
    token = secrets.token_urlsafe(32)
    expires = time.time() + 3600
    with download_ticket_lock:
        for old in [key for key, value in download_tickets.items() if value["expires"] < time.time()]:
            download_tickets.pop(old, None)
        download_tickets[token] = {"user_id": user_id, "kind": kind, "drive": drive, "path": path,
                                  "filename": filename, "expires": expires}
    return token


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if User.query.filter_by(role="admin").first():
        return redirect(url_for("login"))
    token = get_setup_token(create=False)
    if request.method == "POST":
        with setup_lock:
            if User.query.filter_by(role="admin").first():
                return redirect(url_for("login"))
            submitted = request.form.get("token", "")
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            if not token or not hmac.compare_digest(submitted, token):
                flash("The setup token is invalid.", "error")
            elif not re.fullmatch(r"[A-Za-z0-9_.-]{3,80}", username):
                flash("Use a 3–80 character username with letters, numbers, dots, underscores, or hyphens.", "error")
            elif len(password) < 12:
                flash("Use a password with at least 12 characters.", "error")
            else:
                user = User(username=username, password_hash=generate_password_hash(password), role="admin")
                db.session.add(user)
                db.session.commit()
                try:
                    os.remove(os.path.join(app.instance_path, "setup.token"))
                except OSError:
                    pass
                login_user(user)
                return redirect(url_for("index"))
    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    has_admin = User.query.filter_by(role="admin").first() is not None
    if not has_admin:
        ensure_setup_token()
        return redirect(url_for("setup"))
    next_url = safe_next_url(request.values.get("next", ""))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        user = User.query.filter_by(username=username, is_active=True).first()
        valid = user is not None and check_password_hash(user.password_hash, request.form.get("password", ""))
        if valid:
            login_user(user)
            return redirect(safe_next_url(request.form.get("next", "")))
        flash("Username or password is incorrect.", "error")
    return render_template("login.html", next_url=next_url)


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    return redirect(url_for("index"))


@app.route("/admin/users", methods=["GET", "POST"])
@admin_required
def admin_users():
    if request.method == "POST":
        action = request.form.get("action", "create")
        if action == "disable":
            user = db.session.get(User, request.form.get("user_id", type=int))
            active_admins = User.query.filter_by(role="admin", is_active=True).count()
            if user and user.id != current_user.id and not (user.role == "admin" and user.is_active and active_admins <= 1):
                user.is_active = False
                db.session.commit()
                flash(f"Disabled {user.username}.", "success")
            else:
                flash("You cannot disable your own account or the last active admin.", "error")
        else:
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            if not re.fullmatch(r"[A-Za-z0-9_.-]{3,80}", username) or len(password) < 12:
                flash("Enter a valid username and a password of at least 12 characters.", "error")
            elif User.query.filter_by(username=username).first():
                flash("That username already exists.", "error")
            else:
                db.session.add(User(username=username, password_hash=generate_password_hash(password), role="user"))
                db.session.commit()
                flash(f"Created {username}.", "success")
    users = User.query.order_by(User.username).all()
    return render_template("admin_users.html", users=users)


@app.route("/admin/audit")
@admin_required
def admin_audit():
    audit_buffer.flush(app)
    page = max(1, request.args.get("page", 1, type=int))
    query = AuditEvent.query.join(User, isouter=True)
    action = request.args.get("action", "")
    username = request.args.get("username", "").strip()
    path_filter = request.args.get("path", "").strip()
    if action in ("preview", "download", "delete", "delete_attempt", "delete_failed"):
        query = query.filter(AuditEvent.action == action)
    if username:
        query = query.filter(User.username.ilike(f"%{username}%"))
    if path_filter:
        query = query.filter(AuditEvent.path.ilike(f"%{path_filter}%"))
    pagination = query.order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc()).paginate(page=page, per_page=50, error_out=False)
    return render_template("admin_audit.html", events=pagination.items, pagination=pagination,
                           filters={"action": action, "username": username, "path": path_filter})


@app.route("/api/drives")
@login_required
def api_drives():
    return jsonify(drives=[{"id": drive[:2], "label": drive[:2]} for drive in list_fixed_drives()])


@app.route("/api/browse")
@login_required
def browse_files():
    drive = request.args.get("drive", "")
    current = request.args.get("path", "")
    directory = resolve_drive_path(drive, current)
    if not os.path.isdir(directory):
        abort(400, description="path is not a folder")
    root = next(d for d in list_fixed_drives() if d[:2].casefold() == drive.casefold())
    favorite_paths = {
        favorite.path.casefold()
        for favorite in Favorite.query.filter_by(user_id=current_user.id, drive=drive[:2].upper()).all()
    }
    items = []
    try:
        with os.scandir(directory) as scan:
            entries = list(scan)
        def browse_sort(entry):
            try:
                is_directory = entry.is_dir(follow_symlinks=False)
            except OSError:
                is_directory = False
            return (not is_directory, entry.name.casefold())
        entries.sort(key=browse_sort)
        for entry in entries:
            full_path = os.path.join(directory, entry.name)
            try:
                if _is_reparse_point(full_path):
                    continue
                rel = drive_relative_path(root, full_path)
                resolved = resolve_drive_path(drive, rel)
                if entry.is_dir(follow_symlinks=False):
                    try:
                        with os.scandir(resolved):
                            pass
                    except OSError:
                        continue
                    items.append({"name": entry.name, "path": rel, "type": "directory",
                                  "favorite": rel.casefold() in favorite_paths})
                elif entry.is_file(follow_symlinks=False):
                    ext = os.path.splitext(entry.name)[1].lower()
                    kind = "image" if ext in IMAGE_EXTENSIONS else "video" if ext in VIDEO_EXTENSIONS else "file"
                    row = {"name": entry.name, "path": rel, "type": kind,
                           "size": entry.stat(follow_symlinks=False).st_size,
                           "favorite": rel.casefold() in favorite_paths}
                    if kind == "image":
                        row["thumbnail"] = url_for("thumbnail", drive=drive[:2], path=rel)
                    items.append(row)
            except (OSError, PermissionError, HTTPException):
                continue
    except OSError:
        abort(403, description="cannot read folder")
    return jsonify(drive=drive[:2].upper(), path=drive_relative_path(root, directory), items=items)


@app.route("/api/favorites", methods=["POST"])
@login_required
def update_favorite():
    body = request.get_json(silent=True) or {}
    drive = body.get("drive", "")
    relative_path = body.get("path", "")
    is_favorite = body.get("favorite")
    if not isinstance(is_favorite, bool) or not isinstance(relative_path, str) or not relative_path:
        abort(400, description="drive, file or folder path, and favorite state are required")

    normalized_drive = drive[:2].upper() if isinstance(drive, str) else ""
    if normalized_drive not in {d[:2].upper() for d in list_fixed_drives()}:
        abort(400, description="drive is not available")
    if is_favorite:
        resolved = resolve_drive_path(normalized_drive, relative_path)
        if not (os.path.isfile(resolved) or os.path.isdir(resolved)):
            abort(400, description="only files and folders can be favorited")
        root = next(d for d in list_fixed_drives() if d[:2].casefold() == normalized_drive.casefold())
        normalized_path = drive_relative_path(root, resolved).replace("\\", "/")
    else:
        # Permit removing stale favorites even after the source was moved/deleted.
        normalized_path = relative_path.replace("\\", "/").strip("/")
        if not normalized_path or any(part in (".", "..") for part in normalized_path.split("/")):
            abort(400, description="invalid favorite path")
    favorite = Favorite.query.filter_by(user_id=current_user.id, drive=normalized_drive,
                                        path=normalized_path).first()
    if is_favorite and favorite is None:
        db.session.add(Favorite(user_id=current_user.id, drive=normalized_drive,
                                path=normalized_path, item_type="directory" if os.path.isdir(resolved) else "file"))
        db.session.commit()
    elif not is_favorite and favorite is not None:
        db.session.delete(favorite)
        db.session.commit()
    return jsonify(ok=True, favorite=is_favorite, drive=normalized_drive, path=normalized_path)


@app.route("/api/delete", methods=["POST"])
@admin_required
def delete_file():
    body = request.get_json(silent=True) or {}
    drive, relative_path = body.get("drive", ""), body.get("path", "")
    path = resolve_drive_path(drive, relative_path)
    is_directory = os.path.isdir(path)
    if not os.path.isfile(path) and not is_directory:
        abort(400, description="path is not a file or folder")
    normalized_drive = drive[:2].upper()
    root = next(d for d in list_fixed_drives() if d[:2].casefold() == normalized_drive.casefold())
    if is_directory:
        _validate_recursive_delete(path, root)
    normalized_path = drive_relative_path(root, path).replace("\\", "/")

    # Persist the actor before removing the source file so a database error can
    # never silently turn a successful deletion into an unattributed one.
    event = AuditEvent(user_id=current_user.id, action="delete_attempt", drive=normalized_drive,
                       path=normalized_path, created_at=datetime.now(timezone.utc))
    db.session.add(event)
    db.session.commit()
    try:
        if is_directory:
            import shutil
            shutil.rmtree(path)
        else:
            os.remove(path)
    except OSError:
        event.action = "delete_failed"
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.exception("Could not persist failed file deletion status")
        kind = "folder" if is_directory else "file"
        return jsonify(error=f"The {kind} could not be deleted. It may be in use or access may be denied."), 409
    event.action = "delete"
    audit_recorded = True
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        audit_recorded = False
        app.logger.exception("Path was deleted but its audit status could not be finalized")
    return jsonify(ok=True, deleted=True, drive=normalized_drive, path=normalized_path,
                   deleted_type="folder" if is_directory else "file",
                   deleted_by=current_user.username, audit_recorded=audit_recorded)


@app.route("/api/favorites")
@login_required
def list_favorites():
    favorites = Favorite.query.filter_by(user_id=current_user.id).order_by(Favorite.created_at.desc(), Favorite.id.desc()).all()
    roots = {drive[:2].upper(): drive for drive in list_fixed_drives()}
    items = []
    for favorite in favorites:
        drive = favorite.drive[:2].upper()
        relative_path = favorite.path.replace("\\", "/")
        name = relative_path.rsplit("/", 1)[-1] or drive
        item = {"drive": drive, "path": relative_path, "name": name,
                "type": favorite.item_type, "favorite": True, "available": False}
        root = roots.get(drive)
        if root:
            try:
                resolved = resolve_drive_path(drive, relative_path)
                if os.path.isfile(resolved):
                    ext = os.path.splitext(resolved)[1].lower()
                    item["type"] = "image" if ext in IMAGE_EXTENSIONS else "video" if ext in VIDEO_EXTENSIONS else "file"
                    item["size"] = os.path.getsize(resolved)
                    if item["type"] == "image":
                        item["thumbnail"] = url_for("thumbnail", drive=drive, path=relative_path)
                    item["available"] = True
                elif os.path.isdir(resolved):
                    item["type"] = "directory"
                    item["available"] = True
            except (HTTPException, OSError, StopIteration):
                pass
        items.append(item)
    return jsonify(items=items)


def _send_media(drive, relative_path, inline):
    path = resolve_drive_path(drive, relative_path)
    if not os.path.isfile(path):
        abort(400, description="path is not a file")
    ext = os.path.splitext(path)[1].lower()
    if ext not in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS:
        abort(415, description="preview is not supported for this file type")
    mimetype = mimetypes.guess_type(path)[0]
    if ext == ".heic": mimetype = "image/heic"
    if ext == ".heif": mimetype = "image/heif"
    if ext == ".mov": mimetype = "video/quicktime"
    if ext == ".mkv": mimetype = "video/x-matroska"
    response = send_file(path, mimetype=mimetype, as_attachment=not inline, download_name=os.path.basename(path), conditional=True, max_age=0)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "private, no-store"
    return response


@app.route("/api/thumbnail")
@login_required
def thumbnail():
    drive, relative_path = request.args.get("drive", ""), request.args.get("path", "")
    path = resolve_drive_path(drive, relative_path)
    if os.path.splitext(path)[1].lower() not in IMAGE_EXTENSIONS:
        abort(415)
    stat = os.stat(path)
    cache_key = hashlib.sha256(f"{os.path.normcase(path)}:{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8")).hexdigest()
    thumb_path = os.path.join(thumbnail_dir, cache_key + ".jpg")
    if not os.path.exists(thumb_path):
        try:
            with Image.open(path) as image:
                image = ImageOps.exif_transpose(image).convert("RGB")
                image.thumbnail((256, 256), Image.Resampling.LANCZOS)
                image.save(thumb_path, "JPEG", quality=82, optimize=True)
        except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
            abort(415, description="image could not be previewed")
    try:
        os.utime(thumb_path, None)
    except OSError:
        pass
    return send_file(thumb_path, mimetype="image/jpeg", max_age=3600)


def _image_preview_file(path):
    """Return a cached, screen-sized image rendition without altering downloads."""
    stat = os.stat(path)
    cache_key = hashlib.sha256(f"preview:{os.path.normcase(path)}:{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8")).hexdigest()
    try:
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source)
            image.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
            has_alpha = image.mode in ("RGBA", "LA") or "transparency" in image.info
            suffix = ".png" if has_alpha else ".jpg"
            cached_path = os.path.join(thumbnail_dir, cache_key + suffix)
            if not os.path.exists(cached_path):
                temp_path = cached_path + "." + uuid.uuid4().hex + ".tmp"
                if has_alpha:
                    image.convert("RGBA").save(temp_path, "PNG", optimize=True)
                else:
                    image.convert("RGB").save(temp_path, "JPEG", quality=82, optimize=True)
                try:
                    os.replace(temp_path, cached_path)
                finally:
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
            return cached_path, "image/png" if has_alpha else "image/jpeg"
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
        abort(415, description="image could not be previewed")


@app.route("/api/image-preview")
@login_required
def optimized_image_preview():
    path = resolve_drive_path(request.args.get("drive", ""), request.args.get("path", ""))
    if not os.path.isfile(path) or os.path.splitext(path)[1].lower() not in IMAGE_EXTENSIONS:
        abort(415, description="image preview is not supported")
    cached_path, mimetype = _image_preview_file(path)
    try:
        os.utime(cached_path, None)
    except OSError:
        pass
    response = send_file(cached_path, mimetype=mimetype, max_age=3600, conditional=True)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "private, max-age=3600"
    return response


@app.route("/api/preview")
@login_required
def preview_media():
    return _send_media(request.args.get("drive", ""), request.args.get("path", ""), inline=True)


@app.route("/api/audit/preview", methods=["POST"])
@login_required
def audit_preview():
    body = request.get_json(silent=True) or {}
    drive, relative_path = body.get("drive", ""), body.get("path", "")
    path = resolve_drive_path(drive, relative_path)
    if not os.path.isfile(path) or os.path.splitext(path)[1].lower() not in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS:
        abort(415)
    _queue_audit("preview", drive, relative_path)
    endpoint = "optimized_image_preview" if os.path.splitext(path)[1].lower() in IMAGE_EXTENSIONS else "preview_media"
    return jsonify(ok=True, url=url_for(endpoint, drive=drive[:2], path=relative_path))


@app.route("/api/download/ticket", methods=["POST"])
@login_required
def create_file_ticket():
    body = request.get_json(silent=True) or {}
    drive, relative_path = body.get("drive", ""), body.get("path", "")
    path = resolve_drive_path(drive, relative_path)
    if not os.path.isfile(path):
        abort(400, description="path is not a file")
    token = _ticket(current_user.id, "file", drive[:2].upper(), relative_path, os.path.basename(path))
    _queue_audit("download", drive, relative_path)
    return jsonify(url=url_for("download_ticket", token=token), name=os.path.basename(path))


@app.route("/api/download/ticket/<token>")
@login_required
def download_ticket(token):
    with download_ticket_lock:
        ticket = download_tickets.get(token)
    if not ticket or ticket["expires"] < time.time() or ticket["user_id"] != current_user.id:
        abort(404)
    if ticket["kind"] == "archive":
        path = ticket["path"]
        if not os.path.isfile(path): abort(404)
        file_response = send_file(path, mimetype="application/zip", as_attachment=True,
                                  download_name=ticket["filename"], conditional=False, max_age=0)
        headers = dict(file_response.headers)
        headers["X-Content-Type-Options"] = "nosniff"
        headers["Cache-Control"] = "private, no-store"
        def stream_and_remove():
            try:
                yield from file_response.response
            finally:
                file_response.close()
                try: os.remove(path)
                except OSError: pass
        return Response(stream_with_context(stream_and_remove()), headers=headers)
    if ticket["kind"] != "file": abort(404)
    path = resolve_drive_path(ticket["drive"], ticket["path"])
    if not os.path.isfile(path): abort(404)
    response = send_file(path, as_attachment=True, download_name=ticket["filename"], conditional=True, max_age=0)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "private, no-store"
    return response


def collect_drive_archive_entries(drive, base_path, selected_paths):
    base_real = os.path.realpath(base_path)
    root = next(d for d in list_fixed_drives() if d[:2].casefold() == drive.casefold())
    found = {}
    def add_file(source):
        arc = os.path.relpath(source, base_real).replace(os.sep, "/")
        found.setdefault(arc, (source, arc, False, os.path.getsize(source)))
    def walk(directory):
        try:
            children = sorted(os.scandir(directory), key=lambda child: child.name.casefold())
        except OSError:
            abort(403, description="cannot read selected folder")
        for child in children:
            child_path = os.path.join(directory, child.name)
            try:
                if _is_reparse_point(child_path): continue
                rel = drive_relative_path(root, child_path)
                resolve_drive_path(drive, rel)
                arc = os.path.relpath(child_path, base_real).replace(os.sep, "/")
                if child.is_dir(follow_symlinks=False):
                    found.setdefault(arc + "/", (child_path, arc + "/", True, 0))
                    walk(child_path)
                elif child.is_file(follow_symlinks=False):
                    add_file(child_path)
            except (OSError, HTTPException):
                continue
    for selected in selected_paths:
        source = resolve_drive_path(drive, selected)
        try:
            inside = os.path.normcase(os.path.commonpath([base_real, source])) == os.path.normcase(base_real)
        except ValueError:
            inside = False
        if not inside or source == base_real: abort(403, description="selection is outside the current folder")
        if os.path.isdir(source):
            arc = os.path.relpath(source, base_real).replace(os.sep, "/")
            found.setdefault(arc + "/", (source, arc + "/", True, 0))
            walk(source)
        elif os.path.isfile(source): add_file(source)
        else: abort(400, description="selected item is not a file or folder")
    entries = list(found.values())
    if not entries: abort(400, description="selection contains no downloadable files")
    if len(entries) > MAX_ZIP_FILES or sum(entry[3] for entry in entries) > MAX_ZIP_BYTES:
        abort(413, description="selection exceeds ZIP limits")
    return entries


@app.route("/api/download/archive", methods=["POST"])
@login_required
def create_download_archive():
    body = request.get_json(silent=True) or {}
    drive, base, selected = body.get("drive", ""), body.get("base", ""), body.get("paths")
    if not isinstance(selected, list) or not selected or len(selected) > MAX_ZIP_FILES or any(not isinstance(item, str) for item in selected):
        abort(400, description="select one or more files or folders")
    base_path = resolve_drive_path(drive, base)
    if not os.path.isdir(base_path): abort(400, description="base path is not a folder")
    entries = collect_drive_archive_entries(drive, base_path, selected)
    token = secrets.token_urlsafe(32)
    archive_name = f"sbm-download-{uuid.uuid4().hex}.zip"
    os.makedirs(OUTGOING_DIR, exist_ok=True)
    temp_path = os.path.join(OUTGOING_DIR, archive_name + ".tmp")
    archive_path = os.path.join(OUTGOING_DIR, archive_name)
    try:
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            for source, archive_entry, is_dir, _size in entries:
                if is_dir: archive.writestr(archive_entry, "")
                else: archive.write(source, archive_entry)
        os.replace(temp_path, archive_path)
    except Exception:
        for path in (temp_path, archive_path):
            try: os.remove(path)
            except OSError: pass
        raise
    for source, _entry, is_dir, _size in entries:
        if not is_dir:
            _queue_audit("download", drive, drive_relative_path(next(d for d in list_fixed_drives() if d[:2].casefold() == drive.casefold()), source))
    with download_ticket_lock:
        download_tickets[token] = {"user_id": current_user.id, "kind": "archive", "drive": drive[:2].upper(),
                                   "path": archive_path, "filename": f"SBM-files-{token[:8]}.zip", "expires": time.time() + 3600}
    return jsonify(url=url_for("download_ticket", token=token), name=f"SBM-files-{token[:8]}.zip")


@app.route("/init", methods=["POST"])
@csrf.exempt
def init():
    data = request.get_json(force=True)
    upload_id = data.get("id", "")
    if not ID_RE.match(upload_id):
        return jsonify(error="bad id"), 400

    # Already uploaded and still present on the laptop -> tell the browser to skip it.
    if data.get("force"):
        clear_done(upload_id)
    else:
        saved = read_done().get(upload_id)
        if saved and os.path.exists(os.path.join(SAVE_DIR, *saved.split("/"))):
            return jsonify(done=True, saved=saved)

    total = int(data["total"])
    size = int(data["size"])
    d = upload_dir(upload_id)

    # Chunk size changed since the partial upload started -> old chunks no longer line up.
    meta = load_meta(upload_id)
    if meta and meta["total"] != total:
        shutil.rmtree(d, ignore_errors=True)
        meta = None

    os.makedirs(d, exist_ok=True)
    if meta is None:
        meta = {"parts": safe_parts(data.get("name", "")), "size": size, "total": total}
        with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f)
    return jsonify(received=received_chunks(upload_id, meta["total"]))


@app.route("/chunk", methods=["POST"])
@csrf.exempt
def chunk():
    upload_id = request.args.get("id", "")
    meta = load_meta(upload_id)
    if meta is None:
        return jsonify(error="unknown upload"), 404
    try:
        index = int(request.args.get("index", "-1"))
    except ValueError:
        return jsonify(error="bad index"), 400
    if not 0 <= index < meta["total"]:
        return jsonify(error="index out of range"), 400

    d = upload_dir(upload_id)
    tmp_path = os.path.join(d, f"part_{index}.{uuid.uuid4().hex[:6]}.tmp")
    with open(tmp_path, "wb") as f:
        shutil.copyfileobj(request.stream, f, length=1024 * 1024)
    os.replace(tmp_path, os.path.join(d, f"part_{index}"))  # atomic: a part is complete or absent
    return jsonify(ok=True)


@app.route("/complete", methods=["POST"])
@csrf.exempt
def complete():
    body = request.get_json(force=True) or {}
    upload_id = body.get("id", "")
    want_extract = bool(body.get("extract", False))
    delete_zip = bool(body.get("delete_zip", False))

    if not ID_RE.match(upload_id or ""):
        return jsonify(error="unknown upload"), 404
    completion_lock = complete_locks[int(upload_id[:2], 16) % len(complete_locks)]
    with completion_lock:
        meta = load_meta(upload_id)
        if meta is None:
            # Might be a repeated call after a successful assemble.
            saved = read_done().get(upload_id)
            if saved:
                return jsonify(saved=saved)
            return jsonify(error="unknown upload"), 404

        d = upload_dir(upload_id)
        missing = [i for i in range(meta["total"]) if not os.path.exists(os.path.join(d, f"part_{i}"))]
        if missing:
            return jsonify(error="missing chunks", missing=missing), 409

        parts = meta["parts"]
        assembly_started = time.perf_counter()
        assembled = os.path.join(d, "assembled.tmp")
        with open(assembled, "wb") as out:
            for i in range(meta["total"]):
                with open(os.path.join(d, f"part_{i}"), "rb") as part:
                    shutil.copyfileobj(part, out, length=4 * 1024 * 1024)
        written = os.path.getsize(assembled)
        if written != meta["size"]:
            os.remove(assembled)
            return jsonify(error="size mismatch", expected=meta["size"], got=written), 422

        assembly_seconds = time.perf_counter() - assembly_started

        # Recreate the folder structure inside SAVE_DIR (parts are already sanitised).
        dest_dir = os.path.join(SAVE_DIR, *parts[:-1])
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError as e:
            return jsonify(error=f"cannot create folder: {e}"), 409

        # Keep the lock around only name selection and the atomic rename. Large
        # chunk reads/writes above can now run at the same time for other uploads.
        with destination_lock:
            name, final = unique_path(dest_dir, parts[-1])
            os.replace(assembled, final)

        saved_rel = "/".join(parts[:-1] + [name])
        result = {"saved": saved_rel}
        done_rel = saved_rel
        result["timings"] = {"assembly_seconds": round(assembly_seconds, 4)}

        # Optional: unpack zips (iOS "Compress" output) into a real folder.
        if want_extract and name.lower().endswith(".zip") and zipfile.is_zipfile(final):
            extraction_started = time.perf_counter()
            try:
                root, count = extract_zip(final, dest_dir, name[:-4])
                done_rel = "/".join(parts[:-1] + [root])
                result["extracted"] = {"files": count, "root": done_rel}
                if delete_zip:
                    os.remove(final)
            except Exception as e:  # keep the zip so nothing is lost
                result["extract_error"] = str(e)
            finally:
                result["timings"]["extraction_seconds"] = round(time.perf_counter() - extraction_started, 4)

        mark_done(upload_id, done_rel)
        shutil.rmtree(d, ignore_errors=True)
        return jsonify(result)


if __name__ == "__main__":
    with app.app_context():
        migrate_upgrade(directory=os.path.join(os.path.dirname(__file__), "migrations"))
        ensure_setup_token()
    start_audit_flush_worker(app, interval_seconds=300)
    start_thumbnail_cleanup_worker()
    addresses = sorted({item[4][0] for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
                        if not item[4][0].startswith(("127.", "169.254."))})
    if addresses:
        for ip in addresses:
            print(f"Open on iPhone: http://{ip}:8000")
    else:
        print("Open on this laptop: http://127.0.0.1:8000")
    app.run(host="0.0.0.0", port=int(os.getenv("SBM_PORT", "8000")), threaded=True)
