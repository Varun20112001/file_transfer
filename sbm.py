# receive_chunked.py  (pip install flask)
import os, re, json, shutil, socket, uuid, threading, zipfile, time
from flask import Flask, Response, abort, request, jsonify, render_template, send_file, stream_with_context, url_for
from werkzeug.utils import secure_filename

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
ID_RE = re.compile(r"^[0-9a-f]{6,32}$")
# Allow unrelated uploads to assemble concurrently while preventing duplicate
# /complete requests for the same upload from racing each other.
complete_locks = tuple(threading.Lock() for _ in range(64))
destination_lock = threading.Lock()  # protects unique-name selection and final replace
done_lock = threading.Lock()       # protects done.json


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
    return render_template("index.html")


@app.route("/api/browse")
def browse_files():
    current = request.args.get("path", "")
    directory = resolve_save_path(current)
    if not os.path.isdir(directory):
        abort(400, description="path is not a folder")

    items = []
    try:
        with os.scandir(directory) as scan:
            entries = sorted(scan, key=lambda entry: (not entry.is_dir(follow_symlinks=False), entry.name.casefold()))
        for entry in entries:
            if entry.name.casefold() == ".chunks" or entry.is_symlink():
                continue
            full_path = os.path.join(directory, entry.name)
            resolve_save_path(relative_save_path(full_path))
            if entry.is_dir(follow_symlinks=False):
                items.append({"name": entry.name, "path": relative_save_path(full_path), "type": "directory"})
            elif entry.is_file(follow_symlinks=False):
                items.append({
                    "name": entry.name,
                    "path": relative_save_path(full_path),
                    "type": "file",
                    "size": entry.stat(follow_symlinks=False).st_size,
                })
    except OSError:
        abort(403, description="cannot read folder")

    current_rel = "" if os.path.realpath(directory) == os.path.realpath(SAVE_DIR) else relative_save_path(directory)
    return jsonify(path=current_rel, items=items)


@app.route("/api/download/file")
def download_file():
    path = resolve_save_path(request.args.get("path", ""))
    if not os.path.isfile(path):
        abort(400, description="path is not a file")
    response = send_file(path, as_attachment=True, download_name=os.path.basename(path), conditional=True, max_age=0)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/api/download/archive", methods=["POST"])
def create_download_archive():
    body = request.get_json(silent=True) or {}
    base = body.get("base", "")
    selected = body.get("paths")
    if not isinstance(selected, list) or not selected or len(selected) > MAX_ZIP_FILES:
        abort(400, description="select one or more files or folders")
    if any(not isinstance(path, str) for path in selected):
        abort(400, description="selected paths must be strings")

    base_path = resolve_save_path(base)
    if not os.path.isdir(base_path):
        abort(400, description="base path is not a folder")
    entries = collect_archive_entries(base_path, selected)
    token = uuid.uuid4().hex
    archive_name = f"sbm-download-{token}.zip"
    temp_path = os.path.join(OUTGOING_DIR, archive_name + ".tmp")
    archive_path = os.path.join(OUTGOING_DIR, archive_name)
    try:
        with zipfile.ZipFile(temp_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            for source, archive_name_in_zip, is_dir, _size in entries:
                if is_dir:
                    archive.writestr(archive_name_in_zip, "")
                else:
                    archive.write(source, archive_name_in_zip)
        os.replace(temp_path, archive_path)
    except Exception:
        for path in (temp_path, archive_path):
            try:
                os.remove(path)
            except OSError:
                pass
        raise

    return jsonify(url=url_for("download_archive", token=token), name=f"SBM-files-{token[:8]}.zip")


@app.route("/api/download/archive/<token>")
def download_archive(token):
    if not OUTGOING_TOKEN_RE.fullmatch(token or ""):
        abort(404)
    path = os.path.join(OUTGOING_DIR, f"sbm-download-{token}.zip")
    if not os.path.isfile(path):
        abort(404)
    file_response = send_file(path, mimetype="application/zip", as_attachment=True,
                              download_name=f"SBM-files-{token[:8]}.zip", conditional=False, max_age=0)
    headers = dict(file_response.headers)
    headers["X-Content-Type-Options"] = "nosniff"
    headers["Cache-Control"] = "no-store"

    def stream_and_remove():
        try:
            yield from file_response.response
        finally:
            file_response.close()
            try:
                os.remove(path)
            except OSError:
                pass

    return Response(stream_with_context(stream_and_remove()), headers=headers)


@app.route("/init", methods=["POST"])
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
    ip = socket.gethostbyname(socket.gethostname())
    print(f"Open on iPhone: http://{ip}:8000")
    app.run(host="0.0.0.0", port=8000, threaded=True)
