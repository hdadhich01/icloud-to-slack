import json
import os
import tempfile
from datetime import datetime

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

# Load environment variables from .env file
load_dotenv()

# 1) Public iCloud Shared Album URL (loaded from .env file)
ALBUM_URL = os.environ.get("ALBUM_URL")

# 2) Your Slack webhook (loaded from .env file)
SLACK_WEBHOOK = os.environ.get("SLACK_WEBHOOK")

# 3) Slack API token for file uploads (loaded from .env file)
SLACK_API_TOKEN = os.environ.get("SLACK_API_TOKEN")

# 4) Local JSON file to track posted photos
SEEN_FILE = "seen_photos.json"


def load_seen_photos():
    if not os.path.exists(SEEN_FILE):
        return set()
    try:
        with open(SEEN_FILE, "r") as f:
            content = f.read().strip()
            if not content:  # Empty file
                return set()
            return set(json.loads(content))
    except json.JSONDecodeError:
        print(f"⚠️ Warning: Invalid JSON in {SEEN_FILE}, treating as empty")
        return set()


def save_seen_photos(photo_urls):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(photo_urls), f)


def post_to_slack(image_url):
    # Extract basic filename from URL without query parameters
    filename = image_url.split("?")[0].split("/")[-1]

    if SLACK_API_TOKEN:
        try:
            # Download the image
            print(f"⬇️ Downloading image: {image_url}")
            response = requests.get(image_url, stream=True)
            if response.status_code != 200:
                print(f"⚠️ Failed to download image: {response.status_code}")
                return False

            # Create a temporary file for the image
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as temp_file:
                temp_file_path = temp_file.name
                for chunk in response.iter_content(chunk_size=1024):
                    if chunk:
                        temp_file.write(chunk)

            # Try to extract EXIF metadata if PIL is available
            metadata_text = ""
            try:
                from PIL import Image
                from PIL.ExifTags import TAGS

                img = Image.open(temp_file_path)
                exif_data = img._getexif()

                if exif_data:
                    metadata = {}
                    for tag_id, value in exif_data.items():
                        tag = TAGS.get(tag_id, tag_id)
                        if tag in ["DateTimeOriginal", "Make", "Model"]:
                            metadata[tag] = value

                    if "DateTimeOriginal" in metadata:
                        metadata_text += f"📅 Taken: {metadata['DateTimeOriginal']}\n"
                    if "Make" in metadata and "Model" in metadata:
                        metadata_text += (
                            f"📱 Device: {metadata['Make']} {metadata['Model']}\n"
                        )
            except (ImportError, Exception) as e:
                print(f"⚠️ Could not extract EXIF data: {str(e)}")

            # Upload the file to Slack
            print(f"📤 Uploading image to Slack...")
            with open(temp_file_path, "rb") as file_content:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                initial_comment = (
                    f"📸 New photo added to the shared album at {timestamp}"
                )
                if metadata_text:
                    initial_comment += f"\n{metadata_text}"

                files = {"file": (filename, file_content, "image/jpeg")}
                data = {
                    "initial_comment": initial_comment,
                    "filename": filename,
                    "channels": "#icloud-photos-spring2025",  # Change this to your desired channel
                }

                upload_response = requests.post(
                    "https://slack.com/api/files.upload",
                    headers={"Authorization": f"Bearer {SLACK_API_TOKEN}"},
                    files=files,
                    data=data,
                )

                # Clean up temporary file
                os.unlink(temp_file_path)

                if upload_response.status_code == 200 and upload_response.json().get(
                    "ok"
                ):
                    print(f"✅ Posted image file to Slack: {filename}")
                    return True
                else:
                    print(f"⚠️ Slack file upload failed: {upload_response.status_code}")
                    print(f"⚠️ Error: {upload_response.text}")
                    # Fall back to webhook method
        except Exception as e:
            print(f"⚠️ Error uploading file: {str(e)}")
            # Fall back to webhook method

    # Fallback to webhook with just the URL
    print(f"📤 Posting image URL to Slack (webhook fallback)")

    message_text = "📸 *New photo added to the shared album!*"
    if "metadata_text" in locals() and metadata_text:  # Check if metadata was extracted
        message_text += f"\n{metadata_text}"

    payload = {
        "text": message_text,
        "attachments": [
            {
                "fallback": "New photo from shared album",
                "image_url": image_url,
                "text": f"Filename: `{filename}`",
            }
        ],
    }

    r = requests.post(SLACK_WEBHOOK, json=payload)
    if r.status_code == 200:
        print(f"✅ Posted to Slack using webhook: {image_url}")
        return True
    else:
        print(f"⚠️ Slack post failed: {r.status_code}, {r.text}")
        return False


def run_scraper():
    """Launch a headless browser to load the album page & extract photo URLs."""
    print("🚀 Launching Playwright browser...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        print(f"🌐 Navigating to album: {ALBUM_URL}")
        page.goto(ALBUM_URL)

        # Wait a few seconds for JavaScript to populate image elements
        page.wait_for_timeout(5000)  # 5 seconds

        # Extract all <img> src attributes in the DOM
        img_srcs = page.evaluate(
            """() => {
            return Array.from(document.querySelectorAll("img"))
                        .map(img => img.src);
        }"""
        )
        browser.close()

    # Filter iCloud CDN URLs but preserve query parameters
    photos = []
    for src in img_srcs:
        if "icloud" in src and "content.com" in src:
            # Don't remove query parameters - they contain authentication tokens
            photos.append(src)

    unique_photos = list(set(photos))
    print(f"🔍 Found {len(unique_photos)} photo(s) in the album.")
    return unique_photos


def main():
    print("🔎 Checking for new photos...")

    # Check required environment variables
    if not ALBUM_URL:
        print("❌ Error: ALBUM_URL not set in .env file")
        return

    if not SLACK_WEBHOOK:
        print("❌ Error: SLACK_WEBHOOK not set in .env file")
        return

    if not SLACK_API_TOKEN:
        print("⚠️ SLACK_API_TOKEN not set in .env file.")
        print("   Continuing with webhook URL method only...")

    seen = load_seen_photos()
    current = set(run_scraper())
    new_photos = current - seen

    if new_photos:
        print(f"🆕 Found {len(new_photos)} new photo(s)!")
        success_count = 0
        for url in sorted(new_photos):
            if post_to_slack(url):
                success_count += 1

        if success_count > 0:
            save_seen_photos(current)
            print(f"✅ Successfully posted {success_count} photos to Slack")
        else:
            print("⚠️ No photos were successfully posted to Slack")
    else:
        print("✅ No new photos.")


if __name__ == "__main__":
    main()
