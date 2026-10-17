# receive_chunked.py  (pip install flask)
import os, re, json, shutil, socket, uuid, threading, zipfile, time
from flask import Flask, request, jsonify, render_template
from werkzeug.utils import secure_filename

SAVE_DIR = os.getenv("SBM_SAVE_DIR", "D:\\Documents\\transfer")
TMP_DIR = os.path.join(SAVE_DIR, ".chunks")
DONE_FILE = os.path.join(TMP_DIR, "done.json")
MAX_ZIP_FILES = 100_000            # safety limits for zip extraction
MAX_ZIP_BYTES = 50 * 1024 ** 3     # 50 GB uncompressed
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
