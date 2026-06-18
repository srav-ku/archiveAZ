import os
import re
import shutil
import json
import subprocess
import requests
import gspread
import boto3
from botocore.config import Config
from google.oauth2.service_account import Credentials
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from playwright.sync_api import sync_playwright

# ==================== CONFIGURATION ====================
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDS_JSON")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
IA_ACCESS_KEY = os.getenv("IA_ACCESS_KEY")
IA_SECRET_KEY = os.getenv("IA_SECRET_KEY")

IA_IDENTIFIER = "actress-AZ-video-12345" # Change this to your target archive bucket
MAX_DOWNLOAD_WORKERS = 5
# =======================================================

scopes = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
creds_dict = json.loads(GOOGLE_CREDS_JSON)
credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
gc = gspread.authorize(credentials)
sheet = gc.open_by_key(SPREADSHEET_ID).sheet1

def sanitize_filename(name):
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()

def scrape_links_with_unblocked_engine(actress_url):
    video_pages = []
    mp4_links = []

    print(f"[LOG] Launching Playwright engine...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox", 
                "--disable-setuid-sandbox", 
                "--disable-gpu", 
                "--disable-dev-shm-usage"
            ]
        )
        
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"}
        )
        page = context.new_page()

        try:
            print(f"[LOG] Requesting URL via browser context: {actress_url}")
            page.goto(actress_url, wait_until="commit", timeout=45000)
            print("[LOG] Page reached. Sleeping briefly to let DOM stabilize...")
            page.wait_for_timeout(3000)
            
            main_html = page.content()
            print(f"[LOG] Successfully extracted HTML content size: {len(main_html)} characters.")
            soup = BeautifulSoup(main_html, "html.parser")
        except Exception as e:
            print(f"[ERROR] Playwright failed to navigate or extract hub page: {str(e)}")
            browser.close()
            return [], f"Failed to load main hub page: {str(e)}"

        print("[LOG] Processing main page HTML via BeautifulSoup selectors...")
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

        print(f"[LOG] Found {len(video_pages)} video subpages. Processing streams in chronological order...")

        for idx, video_page in enumerate(video_pages, start=1):
            try:
                print(f"  -> [{idx}/{len(video_pages)}] Parsing subpage: {video_page}")
                page.goto(video_page, wait_until="commit", timeout=25000)
                page.wait_for_timeout(1000)
                
                sub_html = page.content()
                page_soup = BeautifulSoup(sub_html, "html.parser")
                
                found_on_page = False
                for a in page_soup.select("a[href]"):
                    href = a.get("href", "")
                    if href.endswith(".mp4") or ".mp4?" in href:
                        if not href.startswith("http"):
                            href = "https://www.aznude.com" + href
                        if href not in mp4_links:
                            mp4_links.append(href)
                            print(f"     [✓] Discovered MP4 resource link: {href}")
                        found_on_page = True
                        break
            except Exception as sub_e:
                print(f"     [!] Failed to extract details from subpage due to exception: {sub_e}")
                continue

        browser.close()
        print(f"[LOG] Playwright engine shut down. Unique video links parsed: {len(mp4_links)}")

    return mp4_links, None

def check_audio_presence(file_path):
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "a",
        "-show_entries", "stream=codec_type", "-of", "json", file_path
    ]
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
    print(f"[LOG] Master Target Frame Dimensions Set: {target_w}x{target_h} HD Canvas")

    standardized_clips = []
    
    for idx, clip in enumerate(clips_list):
        norm_output = os.path.join(temp_dir, f"norm_{idx:03d}.mp4")
        has_audio = check_audio_presence(clip)
        print(f"  [-] Locking tracks & layout sync ({idx+1}/{len(clips_list)}) | Audio Present: {has_audio}")

        # FIX: CRF changed from 12 to 22. Preset set to veryfast. Added -threads 2 to prevent Action Runner freeze.
        if has_audio:
            cmd_norm = [
                "ffmpeg", "-y", "-i", clip,
                "-vf", f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,fps=30,setsar=1",
                "-af", "aresample=async=1",
                "-c:v", "libx264", "-crf", "22", "-preset", "veryfast", "-threads", "2",
                "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "44100",
                "-vsync", "cfr", "-loglevel", "error", norm_output
            ]
        else:
            cmd_norm = [
                "ffmpeg", "-y", "-i", clip,
                "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-vf", f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,fps=30,setsar=1",
                "-af", "aresample=async=1",
                "-c:v", "libx264", "-crf", "22", "-preset", "veryfast", "-threads", "2",
                "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "44100",
                "-shortest", "-vsync", "cfr", "-loglevel", "error", norm_output
            ]

        try:
            res = subprocess.run(cmd_norm, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if res.returncode == 0 and os.path.exists(norm_output):
                standardized_clips.append(norm_output)
            else:
                return False, f"Clip processing failed at index {idx} with error: {res.stderr.decode()}"
        except Exception as e:
            return False, f"Exception during normalization of clip {idx}: {str(e)}"

    standardized_clips.sort()
    list_txt_path = os.path.join(temp_dir, "batch_list.txt")
    with open(list_txt_path, "w", encoding="utf-8") as f:
        for clip_path in standardized_clips:
            f.write(f"file '{os.path.abspath(clip_path)}'\n")

    print(f"[LOG] Merging aligned tracks into final presentation destination file: {output_path}")
    cmd_merge = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_txt_path,
        "-c", "copy", "-vsync", "cfr", "-loglevel", "error", output_path
    ]

    try:
        result = subprocess.run(cmd_merge, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode == 0 and os.path.exists(output_path):
            return True, None
        return False, f"Stitching backend failure: {result.stderr}"
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
            config=Config(signature_version="s3v4", retries={'max_attempts': 3})
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
        return True, None
    except Exception as e:
        return False, str(e)

def main():
    print("[LOG] Script initiated. Grabbing target Google Sheet records...")
    records = sheet.get_all_records()
    print(f"[LOG] Successfully pulled {len(records)} records from spreadsheet.")
    
    max_num = 0
    for r in records:
        try:
            val = int(r.get("Number", 0) or 0)
            if val > max_num:
                max_num = val
        except ValueError:
            continue
    print(f"[LOG] Current highest video index number sequence located: {max_num}")

    for idx, row in enumerate(records, start=2):
        status = str(row.get("Status", "")).strip().lower()
        title = str(row.get("Title", "")).strip()
        url = str(row.get("Link", "")).strip()

        if status in ["success", "failed"]:
            continue
        if not title or not url:
            continue

        print(f"\n========================================================")
        print(f"[+] START PROCESSING ROW {idx}: {title}")
        print(f"========================================================")

        mp4_urls = scrape_links_with_unblocked_engine(url)[0]
        video_count = len(mp4_urls)

        if video_count == 0:
            err = "Zero video links found (Page blocked or element path blank)."
            sheet.update(range_name=f'D{idx}:F{idx}', values=[[video_count, "failed", err]])
            print(f"[-] Skipped row {idx}: {err}")
            continue

        temp_dir = os.path.abspath(f"./temp_worker")
        os.makedirs(temp_dir, exist_ok=True)
        
        download_tasks = []
        for i, mp4_url in enumerate(mp4_urls):
            temp_clip_path = os.path.join(temp_dir, f"clip_{i:03d}.mp4")
            download_tasks.append((mp4_url, temp_clip_path, i))

        print(f"[LOG] Starting concurrent ordered stream extraction pipeline ({video_count} items)...")
        downloaded_clips = [None] * video_count
        download_failed = False
        
        with ThreadPoolExecutor(max_workers=MAX_DOWNLOAD_WORKERS) as download_executor:
            futures = {download_executor.submit(download_single_clip, task): task for task in download_tasks}
            for future in as_completed(futures):
                task_details = futures[future]
                original_pos = task_details[2]
                success, clip_path = future.result()
                if success:
                    downloaded_clips[original_pos] = clip_path
                    print(f"  [✓] Fragment {original_pos+1} downloaded successfully.")
                else:
                    print(f"  [!] Target slice {original_pos+1} download failed.")
                    download_failed = True

        if download_failed or None in downloaded_clips:
            merge_success = False
            err_text = f"Download verification missing or incomplete drops encountered."
        else:
            print(f"[LOG] Initiating alignment and processing for final assembly...")
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
            if os.path.exists(final_output_file):
                os.remove(final_output_file)
            print(f"[-] Row {idx} processing failed during compilation: {err_text}")
            continue

        print(f"[LOG] Dispatching completed block artifact payload to Internet Archive...")
        up_ok, up_err = upload_to_internet_archive(final_output_file)
        
        if os.path.exists(final_output_file):
            os.remove(final_output_file)

        if up_ok:
            sheet.update(range_name=f'C{idx}:F{idx}', values=[[next_assign_num, video_count, "success", ""]])
            print(f"[✓] Row {idx} fully verified and recorded in spreadsheet context! Sequence Index: {next_assign_num}")
            max_num = next_assign_num
        else:
            sheet.update(range_name=f'D{idx}:F{idx}', values=[[video_count, "failed", f"Upload error: {up_err}"]])
            print(f"[ERROR] Row {idx} Upload Failed: {up_err}")

if __name__ == "__main__":
    main()
