"""
Download files from a Google Drive folder, convert them, and upload the
results to another Drive folder. Used by .github/workflows/convert.yml
"""
import argparse
import os
import shutil
import subprocess
import tempfile

import pandas as pd
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
from PIL import Image

SCOPES = ["https://www.googleapis.com/auth/drive"]
GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps."


# ---------- Drive helpers ----------
def get_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["GDRIVE_REFRESH_TOKEN"],
        client_id=os.environ["GDRIVE_CLIENT_ID"],
        client_secret=os.environ["GDRIVE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def list_files(svc, folder_id):
    files, token = [], None
    while True:
        resp = svc.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id, name, mimeType)",
            pageSize=1000,
            pageToken=token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files += resp["files"]
        token = resp.get("nextPageToken")
        if not token:
            return files


def download(svc, file_id, path):
    req = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
    with open(path, "wb") as fh:
        dl = MediaIoBaseDownload(fh, req, chunksize=50 * 1024 * 1024)
        done = False
        while not done:
            _, done = dl.next_chunk()


def upload(svc, path, folder_id):
    media = MediaFileUpload(path, resumable=True)
    return svc.files().create(
        body={"name": os.path.basename(path), "parents": [folder_id]},
        media_body=media,
        fields="id",
        supportsAllDrives=True,
    ).execute()


# ---------- Converters: each takes (src, out_dir) and returns the new file path ----------
def _stem(src):
    return os.path.splitext(os.path.basename(src))[0]


def libreoffice(fmt):
    def run(src, out_dir):
        subprocess.run(
            ["soffice", "--headless", "--convert-to", fmt, "--outdir", out_dir, src],
            check=True, timeout=600,
        )
        return os.path.join(out_dir, f"{_stem(src)}.{fmt}")
    return run


def pillow(fmt, ext):
    def run(src, out_dir):
        out = os.path.join(out_dir, f"{_stem(src)}.{ext}")
        img = Image.open(src)
        if fmt == "JPEG" and img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")  # JPEG has no transparency
        img.save(out, fmt)
        return out
    return run


def ffmpeg(ext, extra=None):
    def run(src, out_dir):
        out = os.path.join(out_dir, f"{_stem(src)}.{ext}")
        cmd = ["ffmpeg", "-y", "-i", src] + (extra or []) + [out]
        subprocess.run(cmd, check=True, timeout=3600)
        return out
    return run


def csv_to_xlsx(src, out_dir):
    out = os.path.join(out_dir, f"{_stem(src)}.xlsx")
    pd.read_csv(src).to_excel(out, index=False)
    return out


def xlsx_to_csv(src, out_dir):
    out = os.path.join(out_dir, f"{_stem(src)}.csv")
    pd.read_excel(src).to_csv(out, index=False)
    return out


# mode -> (accepted input extensions, converter)
MODES = {
    "png-to-jpg":   ((".png",), pillow("JPEG", "jpg")),
    "jpg-to-png":   ((".jpg", ".jpeg"), pillow("PNG", "png")),
    "webp-to-jpg":  ((".webp",), pillow("JPEG", "jpg")),
    "docx-to-pdf":  ((".docx", ".doc"), libreoffice("pdf")),
    "pptx-to-pdf":  ((".pptx", ".ppt"), libreoffice("pdf")),
    "xlsx-to-pdf":  ((".xlsx", ".xls"), libreoffice("pdf")),
    "csv-to-xlsx":  ((".csv",), csv_to_xlsx),
    "xlsx-to-csv":  ((".xlsx",), xlsx_to_csv),
    "mp4-to-mp3":   ((".mp4", ".mov", ".mkv"), ffmpeg("mp3", ["-vn", "-q:a", "2"])),
    "wav-to-mp3":   ((".wav",), ffmpeg("mp3", ["-q:a", "2"])),
    "mp3-to-wav":   ((".mp3",), ffmpeg("wav")),
    "mov-to-mp4":   ((".mov",), ffmpeg("mp4", ["-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac"])),
}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--source", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--mode", required=True, choices=MODES)
    args = p.parse_args()

    exts, converter = MODES[args.mode]
    svc = get_service()

    files = [
        f for f in list_files(svc, args.source)
        if not f["mimeType"].startswith(GOOGLE_NATIVE_PREFIX)
        and f["name"].lower().endswith(exts)
    ]
    print(f"Found {len(files)} matching file(s) for '{args.mode}'")

    ok, failed = 0, []
    for f in files:
        work = tempfile.mkdtemp()
        try:
            src = os.path.join(work, f["name"])
            out_dir = os.path.join(work, "out")
            os.makedirs(out_dir)
            print(f"-> {f['name']}")
            download(svc, f["id"], src)
            out = converter(src, out_dir)
            upload(svc, out, args.target)
            ok += 1
        except Exception as e:
            print(f"   FAILED: {e}")
            failed.append(f["name"])
        finally:
            shutil.rmtree(work, ignore_errors=True)

    print(f"\nDone. {ok} converted, {len(failed)} failed.")
    if failed:
        print("Failed files:", ", ".join(failed))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
