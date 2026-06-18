import os
import re
import shutil
import json
import subprocess
import requests
import gspread
import boto3
import gc
from botocore.config import Config
from google.oauth2.service_account import Credentials
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from playwright.sync_api import sync_playwright

# ==================== CONFIGURATION ====================
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDS_JSON")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")

# Internet Archive S3 Credentials
IA_ACCESS_KEY = os.getenv("IA_ACCESS_KEY")
IA_SECRET_KEY = os.getenv("IA_SECRET_KEY")
IA_IDENTIFIER = "actress-AZ-video-12345"

MAX_DOWNLOAD_WORKERS = 5
# =======================================================

scopes = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
creds_dict = json.loads(GOOGLE_CREDS_JSON)
credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
gc_sheet = gspread.authorize(credentials)
sheet = gc_sheet.open_by_key(SPREADSHEET_ID).sheet1

def sanitize_filename(name):
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()

def scrape_links_with_unblocked_engine(actress_url):
    video_pages = []
    mp4_links = []

    print("[LOG] Launching Playwright engine...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080}
        )
        page = context.new_page()

        try:
            print(f"[LOG] Requesting URL via browser context: {actress_url}")
            page.goto(actress_url, wait_until="commit", timeout=45000)
            page.wait_for_timeout(3000)
            main_html = page.content()
            soup = BeautifulSoup(main_html, "html.parser")
        except Exception as e:
            print(f"[ERROR] Playwright failed: {str(e)}")
            browser.close()
            return [], str(e)

        for container in soup.select("div.single-page_content-container"):
            if not container.select_one("div.single-page-title-wrapper"):
                continue
            for a in container.select("div.media-list-item.video-list-item > a[href]"):
                path = a["href"]
                if not path.startswith("http"):
                    path = "https://www.aznude.com" + path
                if path not in video_pages:
                    video_pages.append(path)

        if not video_pages:
            browser.close()
            return [], None

        print(f"[LOG] Found {len(video_pages)} video subpages. Processing streams...")
        for video_page in video_pages:
            try:
                page.goto(video_page, wait_until="commit", timeout=25000)
                page.wait_for_timeout(1000)
                page_soup = BeautifulSoup(page.content(), "html.parser")
                
                for a in page_soup.select("a[href]"):
                    href = a.get("href", "")
                    if href.endswith(".mp4") or ".mp4?" in href:
                        if not href.startswith("http"):
                            href = "https://www.aznude.com" + href
                        if href not in mp4_links:
                            mp4_links.append(href)
                        break
            except Exception:
                continue
        browser.close()
    return mp4_links, None

def check_audio_presence(file_path):
    cmd = ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_type", "-of", "json", file_path]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        data = json.loads(result.stdout)
        return len(data.get("streams", [])) > 0
    except Exception:
        return False

def merge_large_video_batch(clips_list, output_path, temp_dir):
    if not clips_list:
        return False, "No clips provided for merging."

    target_w, target_h = 1920, 1080
    standardized_clips = []
    
    common_flags = [
        "-c:v", "libx264", "-crf", "22", 
        "-preset", "veryfast", "-maxrate", "4M", "-bufsize", "8M",
        "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "44100"
    ]
    
    for idx, clip in enumerate(clips_list):
        norm_output = os.path.join(temp_dir, f"norm_{idx:03d}.mp4")
        has_audio = check_audio_presence(clip)
        print(f"  [-] Processing segment ({idx+1}/{len(clips_list)}) | Audio: {has_audio}")

        if has_audio:
            cmd_norm = [
                "ffmpeg", "-y", "-i", clip,
                "-vf", f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,fps=30,setsar=1",
                "-af", "aresample=async=1", *common_flags, "-vsync", "cfr", "-loglevel", "error", norm_output
            ]
        else:
            cmd_norm = [
                "ffmpeg", "-y", "-i", clip, "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-vf", f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,fps=30,setsar=1",
                "-af", "aresample=async=1", *common_flags, "-shortest", "-vsync", "cfr", "-loglevel", "error", norm_output
            ]

        try:
            res = subprocess.run(cmd_norm, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if res.returncode == 0 and os.path.exists(norm_output):
                standardized_clips.append(norm_output)
            else:
                return False, f"FFmpeg error at index {idx}: {res.stderr.decode()}"
        except Exception as e:
            return False, str(e)

    standardized_clips.sort()
    list_txt_path = os.path.join(temp_dir, "batch_list.txt")
    with open(list_txt_path, "w", encoding="utf-8") as f:
        for clip_path in standardized_clips:
            f.write(f"file '{os.path.abspath(clip_path)}'\n")

    cmd_merge = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_txt_path, "-c", "copy", "-vsync", "cfr", "-loglevel", "error", output_path]
    try:
        result = subprocess.run(cmd_merge, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return (True, None) if result.returncode == 0 and os.path.exists(output_path) else (False, result.stderr)
    except Exception as e:
        return False, str(e)

def download_single_clip(task):
    url, target_path, original_index = task
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        with requests.get(url, stream=True, timeout=45, headers=headers) as r:
            r.raise_for_status()
            with open(target_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=65536):
                    f.write(chunk)
        return True, target_path
    except Exception:
        return False, target_path

def upload_to_internet_archive(video_path):
    filename = os.path.basename(video_path)
    print(f"[LOG] Uploading to Internet Archive Identifier bucket: {IA_IDENTIFIER}")
    try:
        s3_client = boto3.client(
            "s3", endpoint_url="https://s3.us.archive.org",
            aws_access_key_id=IA_ACCESS_KEY, aws_secret_access_key=IA_SECRET_KEY,
            config=Config(signature_version="s3v4")
        )
        extra_args = {
            "ExtraArgs": {
                "CustomArgs": {
                    "x-amz-auto-make-bucket": "1",
                    "x-archive-queue-derive": "0",
                    "x-archive-meta-mediatype": "movies"
                }
            }
        }
        s3_client.upload_file(video_path, IA_IDENTIFIER, filename, **extra_args)
        print(f"[✓] Successfully archived file: {filename}")
        return True, None
    except Exception as e:
        return False, str(e)

def main():
    print("[LOG] Script started. Querying Google Sheet...")
    records = sheet.get_all_records()
    
    max_num = 0
    for r in records:
        try:
            val = int(r.get("Number", 0) or 0)
            if val > max_num:
                max_num = val
        except ValueError:
            continue

    for idx, row in enumerate(records, start=2):
        status = str(row.get("Status", "")).strip().lower()
        title = str(row.get("Title", "")).strip()
        url = str(row.get("Link", "")).strip()

        if status in ["success", "failed"] or not title or not url:
            continue

        print(f"\n========================================================")
        print(f"[+] ROW {idx} RUNNING: {title}")
        print(f"========================================================")

        mp4_urls = scrape_links_with_unblocked_engine(url)[0]
        video_count = len(mp4_urls)

        if video_count == 0:
            sheet.update(range_name=f'D{idx}:F{idx}', values=[[video_count, "failed", "Zero videos found."]])
            continue

        temp_dir = os.path.abspath(f"./temp_worker")
        os.makedirs(temp_dir, exist_ok=True)
        
        download_tasks = [(mp4_url, os.path.join(temp_dir, f"clip_{i:03d}.mp4"), i) for i, mp4_url in enumerate(mp4_urls)]
        downloaded_clips = [None] * video_count
        download_failed = False
        
        with ThreadPoolExecutor(max_workers=MAX_DOWNLOAD_WORKERS) as download_executor:
            futures = {download_executor.submit(download_single_clip, t): t for t in download_tasks}
            for future in as_completed(futures):
                pos = futures[future][2]
                success, clip_path = future.result()
                if success:
                    downloaded_clips[pos] = clip_path
                else:
                    download_failed = True

        if download_failed or None in downloaded_clips:
            merge_success, err_text = False, "Network download fragment failure."
        else:
            merge_success, merge_err = merge_large_video_batch(downloaded_clips, os.path.abspath(f"./temp_out.mp4"), temp_dir)
            err_text = merge_err if merge_err else ""

        next_assign_num = max_num + 1
        clean_name = sanitize_filename(title)
        final_filename = f"{next_assign_num}. {clean_name}.mp4"
        final_output_file = os.path.abspath(f"./{final_filename}")

        if merge_success and os.path.exists("./temp_out.mp4"):
            shutil.move("./temp_out.mp4", final_output_file)

        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)

        if not merge_success:
            sheet.update(range_name=f'D{idx}:F{idx}', values=[[video_count, "failed", err_text]])
            continue

        up_ok, up_err = upload_to_internet_archive(final_output_file)
        if os.path.exists(final_output_file):
            os.remove(final_output_file)

        if up_ok:
            sheet.update(range_name=f'C{idx}:F{idx}', values=[[next_assign_num, video_count, "success", ""]])
            max_num = next_assign_num
        else:
            sheet.update(range_name=f'D{idx}:F{idx}', values=[[video_count, "failed", f"Upload error: {up_err}"]])

        del downloaded_clips
        gc.collect()

if __name__ == "__main__":
    main()
