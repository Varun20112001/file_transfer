# Send to Laptop (SBM)

A small Flask app for transferring files both ways between a phone or computer browser and a folder on your laptop. It is designed for local-network transfers, including an iPhone connected to the laptop through USB Personal Hotspot.

The browser uploads files in chunks over HTTP. The server stores chunks temporarily, assembles each completed file in the configured destination, and can optionally extract ZIP files. The uploader supports retries, pausing, and resuming unfinished uploads.

## Requirements

- Python 3.9 or newer
- Flask
- A browser on the sending device
- The phone and laptop must be able to reach each other over the same local network, or through a working tethered network connection

## Run on Windows

Open PowerShell in this project folder:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install Flask
python sbm.py
```

The app prints a URL such as `http://192.168.1.25:8000`. Open that URL in the sending device's browser. Keep the Python process running while transferring.

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

1. Open SBM on the phone and tap **Send to phone**.
2. Browse the laptop's transfer folder. Open a folder to browse inside it, or check a folder to include it in a download.
3. Download one selected file in its original format, or select multiple files/folders to receive one ZIP. ZIP contents are stored without recompression and retain their paths relative to the folder currently being browsed.
4. On iPhone, use Safari's download controls to save the file or ZIP to Files. To add photos or videos to Photos, save them from Files.

The download browser can access files under `SBM_SAVE_DIR` except SBM's internal `.chunks` directory. Individual file downloads support HTTP range requests where the browser supports resuming. Temporary multi-file ZIPs are removed when the response stream closes and any leftover SBM-generated archives are cleaned at startup.

## Where files and transfer state are stored

By default, completed files go to `D:\Documents\transfer`. The app creates a `.chunks` subdirectory there for partial uploads and `done.json` for completed-upload records. Incomplete uploads can resume while their chunk data remains in that directory. Successfully completed uploads have their temporary chunk directory removed.

## Network and safety notes

- SBM listens on all network interfaces at port `8000`. Windows Firewall may ask whether Python can accept connections; allow it only on the private network you intend to use.
- The app has no login, encryption, or access control. Anyone who can reach its port may be able to upload files into the destination folder. Use it only on a trusted private network, and stop the server when finished. Do not expose port 8000 to the public internet.
- The **Send to phone** browser can list and download files from the configured transfer folder, so devices that can reach SBM can read those files as well as upload new ones.
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
|-- templates/
|   `-- index.html         # Upload page
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
