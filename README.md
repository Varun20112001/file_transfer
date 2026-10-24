# Send to Laptop (SBM)

A small Flask app for transferring files both ways between a phone or computer browser and a folder on your laptop. It is designed for local-network transfers, including an iPhone connected to the laptop through USB Personal Hotspot.

The browser uploads files in chunks over HTTP. The server stores chunks temporarily, assembles each completed file in the configured destination, and can optionally extract ZIP files. The uploader supports retries, pausing, and resuming unfinished uploads.

## Requirements

- Python 3.9 or newer
- Install the packages in `requirements.txt`
- A browser on the sending device
- The phone and laptop must be able to reach each other over the same local network, or through a working tethered network connection

## Run on Windows

Open PowerShell in this project folder:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python sbm.py
```

On the first start, the laptop console prints a one-time admin setup token. Open the printed URL on the laptop, go to `/setup`, and use that token to create the first admin with a password of at least 12 characters. Admins create and disable later accounts under **Accounts**. Keep the Python process running while transferring.

The default database is SQLite at `instance/sbm.sqlite3`; the signing key and cached thumbnails are also stored under `instance/`. Back up the database if you need to preserve accounts and audit history. To use PostgreSQL, install the included psycopg driver and set `SBM_DATABASE_URL` before starting, for example:

```powershell
$env:SBM_DATABASE_URL = 'postgresql+psycopg://sbm_user:password@localhost:5432/sbm'
python sbm.py
```

You can set a stable `SBM_SECRET_KEY` in the environment. If omitted, SBM creates a persistent local key in `instance/secret.key`; keep that file when restoring the database so existing sessions remain valid.

`python sbm.py` applies the checked-in Flask-Migrate revisions before serving. When changing the models, create and apply a revision with:

```powershell
python -m flask --app sbm db migrate -m "Describe schema change"
python -m flask --app sbm db upgrade
```

## Windows tray app and portable package

The tray launcher keeps the SBM server running in the background. Right-click its notification-area icon for **Open admin**, **Show phone QR code**, or **Exit SBM**. The QR window lists the laptop's detected IPv4 addresses; select the address reachable from the phone, such as the USB Personal Hotspot adapter, then scan it with the phone camera. The first packaged launch shows the one-time administrator setup token and a button to open the setup page.

To build the portable ZIP:

```powershell
.\.venv\Scripts\Activate.ps1
.\packaging\build.ps1
```

The output is `dist\SBM-Portable.zip`. Extract it and start `SBM.exe`; the server appears in the Windows notification area. Keep the `data` folder created beside the executable when replacing the app files.

Packaged builds store the database, signing key, cached thumbnails, and log in a writable user-data folder: beside `SBM.exe` in portable mode, or `%LOCALAPPDATA%\SBM` after installation. Your transferred files stay in `SBM_SAVE_DIR` (default `D:\Documents\transfer`). The tray app continues to listen on port 8000; allow SBM through Windows Firewall on the private network used by the phone. Exit from the tray menu to stop the server cleanly.

The portable package includes Python and the required application files. No Python installation is needed on the target laptop. This is a Windows build and should be rebuilt after changing Python dependencies, templates, static assets, or migrations.

To save somewhere other than the default `D:\Documents\transfer`, set `SBM_SAVE_DIR` before starting the app:

```powershell
$env:SBM_SAVE_DIR = 'D:\Documents\transfer'
python sbm.py
```

`sbm.py` reads the `SBM_SAVE_DIR` environment variable directly. It does not load a `.env` file by itself.

## Transfer from an iPhone

1. Connect the iPhone and laptop to the same Wi-Fi network, or connect them with a USB cable and enable **Personal Hotspot** on the iPhone.
2. If Windows prompts you to trust or allow the connected device, complete that prompt. Windows may require Apple Devices or iTunes for USB Personal Hotspot.
3. Start SBM on the laptop and open the printed address in Safari on the iPhone. If that address does not load over USB, find the laptop's active tethered-network IPv4 address with `ipconfig` and use `http://<address>:8000`.
4. Tap **Choose files**, select files in the iPhone picker, then tap **Upload**.

The USB Hotspot option carries SBM's HTTP network traffic over the USB network connection; it is not SMB file sharing or direct USB storage access.

## Use the web interface

- **Choose files** adds selected files to the upload queue. **Choose folder** is available in browsers that support folder selection.
- On iPhone, to send the contents of a folder, select its files in the Files picker and use **Save these files in a new folder** to recreate a destination folder on the laptop.
- **Upload** starts or resumes queued transfers. **Pause** stops active requests; uploaded chunks remain available to resume later.
- **Parallel chunks** controls how many chunk requests are sent at once. **Chunk size** offers 2, 5, 10, 20, and 32 MB. Tune these for your phone, connection, and destination drive; there is no universally fastest setting.
- **Extract .zip files on the laptop** unpacks ZIP uploads. The ZIP is deleted after successful extraction when **Delete the .zip after extracting** is checked.
- **Re-send files that were already uploaded** bypasses the completed-upload skip record.

The page displays upload rate and an end-to-end average. The server reports assembly time separately; ZIP extraction time is also shown when applicable.

## Send files from the laptop to the phone

1. Open SBM on the phone and tap **Send to phone**, then sign in with an account created by the admin.
2. Choose an accessible fixed drive such as `C:` or `D:` and browse folders. Removable and mapped/network drives are excluded. SBM skips folders and files the process cannot access. Tap a row to open a folder or preview a photo/video; tapping a non-media file starts its download.
3. JPEG, PNG, WebP, and HEIC/HEIF photos show cached thumbnails. Tap **Favorites** to revisit starred files and folders across drives; unavailable entries can be removed from the list. Image previews use a cached rendition capped at 2048 pixels on the longest edge, while **Save to phone** always sends the original. Videos open in the browser's native player when the format and codec are supported. SBM does not create video thumbnails, install FFmpeg, or transcode media.
4. Use **Save to phone** for one original file. Select multiple files or a folder to receive one ZIP; ZIP contents are stored without recompression and retain paths relative to the current folder.
5. Safari downloads appear in Files. To add photos or videos to Photos, save them from Files.

Admins can delete individual files with the **×** control at the right of a file row. SBM asks for confirmation, does not allow deleting folders from this browser, and records the signed-in admin, drive, path, and time in the audit history. Failed removals and interrupted audit outcomes are marked separately. Deletion is permanent.

Direct file downloads use HTTP range responses where supported. SBM creates a short-lived user-bound ticket and records one download request when the user starts the download; range/resume requests do not create duplicate audit rows. Preview and ZIP requests are audited, with one row per file included in a ZIP. The admin **Audit** page can filter and page through history. Most audit events are bulk-written every 20 records or five minutes and flushed before the audit page loads and on clean shutdown; delete events are written immediately before a removal and their outcome is updated afterward. An abrupt process crash can leave a delete outcome marked pending or lose other events still waiting in memory. A download request does not prove that the phone saved the full file.

Multi-file ZIPs are staged in SBM's outgoing temporary folder and removed when the response stream closes; leftover SBM-generated archives are cleaned at startup.

Image thumbnails and preview renditions are cached under the SBM instance data folder. SBM removes cache files that have not been used for seven days at startup and every 24 hours; they are regenerated the next time a photo is viewed.

## Where files and transfer state are stored

By default, completed files go to `D:\Documents\transfer`. The app creates a `.chunks` subdirectory there for partial uploads and `done.json` for completed-upload records. Incomplete uploads can resume while their chunk data remains in that directory. Successfully completed uploads have their temporary chunk directory removed.

## Network and safety notes

- SBM listens on all network interfaces at port `8000`. Windows Firewall may ask whether Python can accept connections; allow it only on the private network you intend to use.
- The upload flow remains available without login for compatibility. The drive browser, previews, downloads, accounts, and audit history require an account. Run SBM only on a trusted private network and do not expose port 8000 to the public internet.
- Drive browsing is limited to files accessible to the SBM process. It does not elevate permissions. Path traversal, symlinks/junctions, and SBM's internal state are excluded.
- Login sessions and passwords travel over the existing HTTP connection. Use this only on a trusted local connection; SBM does not add HTTPS.
- Transfers use HTTP. The server sanitizes uploaded path components and extracts ZIP entries within the destination tree. ZIP extraction is limited to 100,000 files and 50 GB of uncompressed content.
- Media files such as JPEG photos and common video formats are already compressed, so this app does not gzip uploaded files.

## Run the API regression tests

```powershell
python -m unittest discover -s tests -v
```

## Project layout

```text
file_transfer/
|-- sbm.py                 # Flask routes and upload/assembly logic
|-- database.py            # SQLAlchemy accounts and batched audit writer
|-- requirements.txt       # Runtime dependencies
|-- migrations/            # Flask-Migrate schema history
|-- templates/
|   |-- index.html         # Transfer and drive browser UI
|   `-- ...               # Login and admin pages
`-- static/
    |-- css/sbm.css        # Page styles
    `-- js/sbm.js          # File picker, chunking, retries, and progress
```

## Troubleshooting

- **The page does not load:** Confirm the server is running, the device can reach the laptop over the selected network, and Windows Firewall permits private-network connections to Python. Try the correct laptop address from `ipconfig`.
- **The USB cable is connected but the page does not load:** Confirm Personal Hotspot is enabled and Windows recognizes the iPhone's tethered network adapter. The cable alone does not establish the network connection.
- **A transfer pauses or fails:** Tap **Upload** again to retry. Existing chunks are detected by the server and skipped during resume.
- **A file is skipped:** Its upload identifier is recorded in `.chunks/done.json`. Enable **Re-send files that were already uploaded** to send it again.
- **ZIP extraction fails:** The original ZIP is retained if extraction reports an error. Check that the archive is valid and within the configured extraction limits.
