#!/usr/bin/env python3
"""
sync_monday_images.py
─────────────────────
Fetches render images from the TFCF Monday catalog board and
commits them to a GitHub repository as:
  images/{plan-slug}-render.jpg
 
Usage:
  1. Copy .env.example to .env and fill in your tokens
  2. pip install requests python-dotenv PyGithub
  3. python sync_monday_images.py
 
GitHub Actions will run this automatically on a schedule.
"""
 
import os
import re
import sys
import requests
from dotenv import load_dotenv
from github import Github, GithubException
 
load_dotenv()
 
# ── Config ────────────────────────────────────────────────────────────────────
MONDAY_API_TOKEN  = os.getenv("MONDAY_API_TOKEN")
GITHUB_TOKEN      = os.getenv("GITHUB_TOKEN")
GITHUB_REPO       = os.getenv("GITHUB_REPO")          # e.g. "your-username/catalog-assets"
CATALOG_BOARD_ID  = "9938332033"
RENDER_COLUMN_ID  = "file_mkvcehhr"                   # Monday "Render" file column ID
IMAGES_FOLDER     = "images"
MONDAY_API_URL    = "https://api.monday.com/v2"
# ──────────────────────────────────────────────────────────────────────────────
 
 
def validate_env():
    missing = [k for k in ["MONDAY_API_TOKEN", "GITHUB_TOKEN", "GITHUB_REPO"]
               if not os.getenv(k)]
    if missing:
        print(f"❌  Missing environment variables: {', '.join(missing)}")
        print("    Copy .env.example to .env and fill in your values.")
        sys.exit(1)
 
 
def to_slug(name: str) -> str:
    """Convert a Monday plan name to a filename slug."""
    s = name.lower()
    s = s.replace("&", "and")
    s = re.sub(r"[(),']+", "", s)       # remove parens, commas, apostrophes
    s = re.sub(r"\s+", "-", s.strip())  # spaces → hyphens
    s = re.sub(r"-+", "-", s)           # collapse multiple hyphens
    return s.strip("-")
 
 
def monday_query(query: str) -> dict:
    """Execute a Monday GraphQL query."""
    response = requests.post(
        MONDAY_API_URL,
        json={"query": query},
        headers={
            "Authorization": MONDAY_API_TOKEN,
            "Content-Type": "application/json",
            "API-Version": "2024-01"
        },
        timeout=30
    )
    response.raise_for_status()
    data = response.json()
    if "errors" in data:
        raise RuntimeError(f"Monday API error: {data['errors']}")
    return data
 
 
def get_catalog_items() -> list:
    """Fetch all items from the catalog board with their file assets."""
    print(f"📋  Fetching catalog board {CATALOG_BOARD_ID}...")
    all_items = []
    cursor = None
 
    while True:
        cursor_arg = f', cursor: "{cursor}"' if cursor else ""
        query = f"""
        {{
          boards(ids: [{CATALOG_BOARD_ID}]) {{
            items_page(limit: 100{cursor_arg}) {{
              cursor
              items {{
                id
                name
                column_values(ids: ["file_mkvcehhr"]) {{
                  ... on FileValue {{
                    files {{
                      asset_id
                      name
                      public_url: url
                    }}
                  }}
                }}
              }}
            }}
          }}
        }}
        """
        data = monday_query(query)
        page = data["data"]["boards"][0]["items_page"]
        all_items.extend(page["items"])
        cursor = page.get("cursor")
        if not cursor:
            break
        print(f"   Fetched {len(all_items)} items so far...")
 
    print(f"   ✓ {len(all_items)} catalog items found")
    return all_items
 
 
def get_asset_url(asset_id: str) -> str | None:
    """Get a downloadable public URL for a Monday asset."""
    query = f"""
    {{
      assets(ids: [{asset_id}]) {{
        public_url
        url
        name
      }}
    }}
    """
    data = monday_query(query)
    assets = data["data"].get("assets", [])
    if not assets:
        return None
    # public_url is a temporary signed URL — valid for a few hours
    return assets[0].get("public_url") or assets[0].get("url")
 
 
def download_image(url: str) -> bytes | None:
    """Download image bytes from a URL."""
    try:
        resp = requests.get(
            url,
            headers={"Authorization": MONDAY_API_TOKEN},
            timeout=30
        )
        resp.raise_for_status()
        if "image" in resp.headers.get("Content-Type", ""):
            return resp.content
        print(f"   ⚠️  URL did not return an image: {url[:60]}")
        return None
    except requests.RequestException as e:
        print(f"   ⚠️  Download failed: {e}")
        return None
 
 
def sync_to_github(images: dict) -> None:
    """
    Commit images to GitHub.
    images = { "images/la-solana-render.jpg": <bytes>, ... }
    Only uploads files that are new or have changed.
    """
    print(f"\n🐙  Connecting to GitHub repo: {GITHUB_REPO}")
    gh   = Github(GITHUB_TOKEN)
    repo = gh.get_repo(GITHUB_REPO)
    ref  = repo.get_git_ref("heads/main")
 
    added   = 0
    updated = 0
    skipped = 0
 
    for path, content in images.items():
        try:
            existing = repo.get_contents(path)
            # Compare SHA to avoid unnecessary commits
            import hashlib, base64
            new_sha = hashlib.sha1(
                f"blob {len(content)}\0".encode() + content
            ).hexdigest()
            if existing.sha == new_sha:
                skipped += 1
                continue
            repo.update_file(
                path,
                f"Update {path}",
                content,
                existing.sha,
                branch="main"
            )
            print(f"   ↑ Updated: {path}")
            updated += 1
        except GithubException as e:
            if e.status == 404:
                repo.create_file(
                    path,
                    f"Add {path}",
                    content,
                    branch="main"
                )
                print(f"   + Added:   {path}")
                added += 1
            else:
                print(f"   ✗ Error on {path}: {e}")
 
    print(f"\n✅  Sync complete — {added} added, {updated} updated, {skipped} unchanged")
 
 
def main():
    validate_env()
    items = get_catalog_items()
 
    images_to_sync = {}
    missing        = []
 
    for item in items:
        plan_name = item["name"].strip()
        if not plan_name or plan_name.lower() == "none":
            continue
 
        slug = to_slug(plan_name)
        col  = item.get("column_values", [])
        files = col[0].get("files", []) if col else []
 
        if not files:
            missing.append(plan_name)
            continue
 
        # Use the first file in the Render column
        asset = files[0]
        asset_id = asset.get("asset_id")
        if not asset_id:
            missing.append(plan_name)
            continue
 
        print(f"   ⬇  Downloading render for: {plan_name}")
        url = get_asset_url(str(asset_id))
        if not url:
            missing.append(plan_name)
            continue
 
        img_bytes = download_image(url)
        if img_bytes:
            filename = f"{IMAGES_FOLDER}/{slug}-render.jpg"
            images_to_sync[filename] = img_bytes
        else:
            missing.append(plan_name)
 
    if missing:
        print(f"\n⚠️  No render found for {len(missing)} plans:")
        for m in missing:
            print(f"    - {m}")
 
    if images_to_sync:
        sync_to_github(images_to_sync)
    else:
        print("\n⚠️  No images to sync.")
 
 
if __name__ == "__main__":
    main()
 
