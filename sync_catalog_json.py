#!/usr/bin/env python3
"""
sync_catalog_json.py
────────────────────
Fetches the TFCF catalog board from Monday.com and commits
catalog.json to the GitHub repository at:
  catalog.json

The app fetches this file at runtime via CATALOG_GITHUB_URL.
Monday credentials never touch the app or proxy.

Designed to mirror sync_monday_images.py conventions exactly.

Usage:
  1. Copy .env.example to .env and fill in your tokens
  2. pip install requests python-dotenv PyGithub
  3. python sync_catalog_json.py

GitHub Actions runs this on a schedule (see .github/workflows/sync-catalog.yml).

Column mapping (board 9938332033):
  name                  → name
  text_mkvnr32v         → designNumber
  color_mm0q2xxk        → type              (status: Main / Accessory)
  color_mkyrtwgf        → status            (Current Phase: AHJ - Approved / PC - Plan Check / CD / SD / etc.)
  numeric_mkvbbxbv      → grossSF
  numeric_mm0q2d4e      → livableSF
  numeric_mm1bxp3p      → widthFt
  text_mkvgnxaj         → lengthFt          (text field, not numeric)
  numeric_mkvbkb7c      → bedrooms
  numeric_mkvb2bw7      → bathrooms
  dropdown_mkvnkg2h     → style
  color_mkyht2q7        → jurisdiction      (status: Altadena / Palisades / both)
  dropdown_mkvbac06     → permittingJurisdiction
  link_mkvcth2r         → factsheetUrl      (link field)
  link_mm3a1xdk         → websiteUrl        (link field)
  formula_mm0qdzgv      → licenseFee        (formula — dollar amount)
  color_mkyrtwgf        → currentPhase      (same field — stored for reference)
  color_mkyvv8jk        → approvedStatus    (status)
  date_mkzvxjd3         → ahjDate
  text_mkvbd7bd         → footprintNote     (free text, e.g. "30x50")
  board_relation_mkwb8d4b → portfolio       (relation — use linked item name)
"""

import os
import re
import sys
import json
import hashlib
import requests
from dotenv import load_dotenv
from github import Github, GithubException

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
MONDAY_API_TOKEN = os.getenv("MONDAY_API_TOKEN")
GITHUB_TOKEN     = os.getenv("GITHUB_TOKEN")
GITHUB_REPO      = os.getenv("GITHUB_REPO")        # e.g. "Mack-TFCF/catalog-assets"
CATALOG_BOARD_ID = "9938332033"
MONDAY_API_URL   = "https://api.monday.com/v2"
OUTPUT_PATH      = "catalog.json"                  # path in the GitHub repo

# Columns to fetch (everything the plan finder needs)
COLUMN_IDS = [
    "text_mkvnr32v",          # Design Number
    "color_mm0q2xxk",         # Type (Main / ADU / Garage)
    "color_mkyrtwgf",         # Current Phase (AHJ - Approved / PC - Plan Check / etc.)
    "numeric_mkvbbxbv",       # Gross SF
    "numeric_mm0q2d4e",       # Livable SF
    "numeric_mm1bxp3p",       # Width (Ft)
    "text_mkvgnxaj",          # Length (Ft) — text field
    "numeric_mkvbkb7c",       # Bedrooms
    "numeric_mkvb2bw7",       # Bathrooms
    "dropdown_mkvnkg2h",      # Style
    "color_mkyht2q7",         # Jurisdiction (LA County / LA City)
    "dropdown_mkvbac06",      # Permitting Jurisdiction
    "link_mkvcth2r",          # Current Factsheet Link
    "link_mm3a1xdk",          # Website URL
    "formula_mm0qdzgv",       # Licence Fee
    "color_mkzvv8jk",         # Approved status
    "date_mkzvxjd3",          # AHJ Date
    "text_mkvbd7bd",          # Footprint (free text)
    "board_relation_mkwb8d4b", # Portfolio (linked board)
]

# ── Style normalisation ───────────────────────────────────────────────────────
# Maps Monday dropdown values → the canonical style names used in the app's
# scoring function and style selector buttons.
# Monday has a richer vocabulary; we fold variants into the nearest app category.
# Plans with styles not listed here keep their original Monday value (stored as-is
# in normalizedStyle) and will match "Open to Any" in the scorer.
STYLE_MAP = {
    # Craftsman family
    "Craftsman":              "Craftsman",
    "Crafrsman Bungalow":     "Craftsman",   # typo in Monday board
    "Calif. Bungalow":        "Craftsman",
    "Bungalow":               "Craftsman",
    # Spanish family
    "Spanish":                "Spanish",
    "Spanish/Mediterranean":  "Spanish",
    "Spanish Colonial":       "Spanish",
    # Ranch family
    "Ranch":                  "Ranch",
    "Calif. Ranch":           "Ranch",
    # Modern family
    "Modern":                 "Modern",
    "MCM":                    "Modern",      # Mid-Century Modern
    # Tudor / Cottage family  (app button: "Tudor Cottage")
    "Tudor":                  "Tudor Cottage",
    "English Cottage":        "Tudor Cottage",
    "Traditional Cottage":    "Tudor Cottage",
    # Colonial
    "Colonial":               "Colonial",
    # Minimal Traditional / Farmhouse — closest fit
    "Minimal Traditional":    "Craftsman",
    "Minimal Farmhouse":      "Ranch",
    # Multi-style — store the primary style
    "Spanish, Ranch, Craftsman": "Spanish",
    # No mapping — kept as-is: Shotgun
}

# ── Jurisdiction normalisation ────────────────────────────────────────────────
# Monday labels: "LA County" = Altadena area, "LA City" = Palisades area.
# The scoring function checks for "altadena" / "palisades" substrings.
JURISDICTION_MAP = {
    "LA County": "Altadena",
    "LA City":   "Palisades",
}
# ──────────────────────────────────────────────────────────────────────────────


def validate_env():
    missing = [k for k in ["MONDAY_API_TOKEN", "GITHUB_TOKEN", "GITHUB_REPO"]
               if not os.getenv(k)]
    if missing:
        print(f"❌  Missing environment variables: {', '.join(missing)}")
        print("    Copy .env.example to .env and fill in your values.")
        sys.exit(1)


def to_slug(name: str) -> str:
    """Convert a plan name to a URL/filename slug. Matches planToSlug() in the app."""
    s = name.lower()
    s = s.replace("&", "and")
    s = re.sub(r"[(),']+", "", s)
    s = re.sub(r"\s+", "-", s.strip())
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def monday_query(query: str) -> dict:
    """Execute a Monday GraphQL query."""
    response = requests.post(
        MONDAY_API_URL,
        json={"query": query},
        headers={
            "Authorization": MONDAY_API_TOKEN,
            "Content-Type": "application/json",
            "API-Version": "2024-01",
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    if "errors" in data:
        raise RuntimeError(f"Monday API error: {data['errors']}")
    return data


def extract_col(column_values: list, col_id: str) -> str:
    """Extract plain text from a column_value entry."""
    col = next((c for c in column_values if c["id"] == col_id), None)
    if not col:
        return ""
    # text field is the most reliable plain-text representation
    if col.get("text"):
        return col["text"].strip()
    # fall back to parsing value JSON for types that don't populate text
    raw = col.get("value")
    if raw:
        try:
            v = json.loads(raw)
            return str(v.get("text") or v.get("name") or v.get("url") or "").strip()
        except (json.JSONDecodeError, AttributeError):
            pass
    return ""


def extract_link(column_values: list, col_id: str) -> str:
    """
    Extract a clean URL from a Monday link column.
    Link columns store { "url": "https://...", "text": "Label" } in value JSON.
    The .text field on the column_value is the display label, not the URL.
    """
    col = next((c for c in column_values if c["id"] == col_id), None)
    if not col:
        return ""
    raw = col.get("value")
    if raw:
        try:
            v = json.loads(raw)
            url = v.get("url", "").strip()
            if url and url.startswith("http"):
                return url
        except (json.JSONDecodeError, AttributeError):
            pass
    # fallback: scan text field for a URL
    text = col.get("text", "")
    match = re.search(r"https?://\S+", text)
    return match.group(0).rstrip(".,)") if match else ""


def extract_number(column_values: list, col_id: str) -> float | None:
    """Extract a numeric value, returning None if empty."""
    val = extract_col(column_values, col_id)
    if not val:
        return None
    try:
        return float(val)
    except ValueError:
        return None


def extract_portfolio_name(column_values: list, col_id: str) -> str:
    """
    Extract the linked item name from a board_relation column.
    board_relation .text is a comma-separated list of linked item names.
    """
    col = next((c for c in column_values if c["id"] == col_id), None)
    if not col:
        return ""
    text = (col.get("text") or "").strip()
    if not text:
        return ""
    # Take the first linked item name if multiple are linked
    return text.split(",")[0].strip()


def get_catalog_items() -> list:
    """Fetch all items from the catalog board with pagination."""
    print(f"📋  Fetching catalog board {CATALOG_BOARD_ID}...")
    all_items = []
    cursor = None
    page_count = 0
    MAX_PAGES = 20

    col_ids_json = json.dumps(COLUMN_IDS)

    while True:
        if page_count >= MAX_PAGES:
            raise RuntimeError(f"Exceeded {MAX_PAGES} pages — possible API loop.")
        cursor_arg = f', cursor: "{cursor}"' if cursor else ""
        query = f"""
        {{
          boards(ids: [{CATALOG_BOARD_ID}]) {{
            items_page(limit: 100{cursor_arg}) {{
              cursor
              items {{
                id
                name
                column_values(ids: {col_ids_json}) {{
                  id
                  text
                  value
                }}
              }}
            }}
          }}
        }}
        """
        data = monday_query(query)
        board = data["data"]["boards"][0]
        page = board["items_page"]
        all_items.extend(page["items"])
        cursor = page.get("cursor")
        page_count += 1
        if not cursor:
            break
        print(f"   Fetched {len(all_items)} items so far...")

    print(f"   ✓ {len(all_items)} raw items fetched")
    return all_items


def determine_primary_variant(items_by_portfolio: dict) -> set:
    """
    For each portfolio group, choose the single primary variant to surface
    in the plan finder. Preference order:
      1. Has widthFt + grossSF + factsheetUrl  (most complete)
      2. Has widthFt + grossSF
      3. Has grossSF only
      4. First item alphabetically
    Returns a set of Monday item IDs that are the primary variant.
    """
    primary_ids = set()
    for portfolio, items in items_by_portfolio.items():
        if len(items) == 1:
            primary_ids.add(items[0]["_id"])
            continue

        def completeness(item):
            score = 0
            if item.get("widthFt"):        score += 4
            if item.get("grossSF"):        score += 3
            if item.get("factsheetUrl"):   score += 2
            if item.get("livableSF"):      score += 1
            return score

        best = max(items, key=completeness)
        primary_ids.add(best["_id"])
    return primary_ids


# Plans must be in one of these statuses to appear in the app.
# Values match the exact label text in the Current Phase column (color_mkyrtwgf).
# Everything else (CD, DD, SD, CP, ZC, blank) is excluded entirely.
ACTIVE_STATUSES = {"AHJ - Approved", "PC - Plan Check"}


def transform_items(raw_items: list) -> list:
    """Transform raw Monday items into the catalog JSON schema."""
    results = []
    items_by_portfolio = {}
    excluded_count = 0

    for item in raw_items:
        name = item["name"].strip()
        if not name or name.lower() == "none":
            continue

        # Skip obvious test/placeholder entries
        name_lower = name.lower()
        if (name_lower.startswith("test") or
                name_lower.startswith("placeholder") or
                name_lower.startswith("do not use")):
            excluded_count += 1
            continue
                  
        cv = item["column_values"]

        # ── Extract all fields ──────────────────────────────────────────────
        design_number   = extract_col(cv, "text_mkvnr32v")
        item_type       = extract_col(cv, "color_mm0q2xxk")       # Type (Main / Accessory)
        status          = extract_col(cv, "color_mkyrtwgf")        # Current Phase (AHJ - Approved / PC - Plan Check / etc.)
        gross_sf        = extract_number(cv, "numeric_mkvbbxbv")
        livable_sf      = extract_number(cv, "numeric_mm0q2d4e")
        width_ft        = extract_number(cv, "numeric_mm1bxp3p")
        length_ft_raw   = extract_col(cv, "text_mkvgnxaj")         # text field
        bedrooms        = extract_number(cv, "numeric_mkvbkb7c")
        bathrooms       = extract_number(cv, "numeric_mkvb2bw7")
        # ── Normalise style and jurisdiction to app-canonical values ────────
        raw_style    = extract_col(cv, "dropdown_mkvnkg2h")
        norm_style   = STYLE_MAP.get(raw_style, raw_style) if raw_style else None

        raw_juris    = extract_col(cv, "color_mkyht2q7")
        perm_juris   = extract_col(cv, "dropdown_mkvbac06")
        # Prefer the Jurisdiction status column; fall back to Permitting Jurisdiction
        best_juris_raw = raw_juris or perm_juris or ""
        norm_juris   = JURISDICTION_MAP.get(best_juris_raw, best_juris_raw) or None
        factsheet_url   = extract_link(cv, "link_mkvcth2r")
        website_url     = extract_link(cv, "link_mm3a1xdk")
        license_fee     = extract_col(cv, "formula_mm0qdzgv")
        approved_status = extract_col(cv, "color_mkzvv8jk")
        ahj_date        = extract_col(cv, "date_mkzvxjd3")
        footprint_note  = extract_col(cv, "text_mkvbd7bd")
        portfolio       = extract_portfolio_name(cv, "board_relation_mkwb8d4b")

        # ── Exclude plans not in an active permitting status ────────────────
        # Only Approved and Plan Check plans appear in the app.
        if status not in ACTIVE_STATUSES:
            excluded_count += 1
            continue

        # ── Derive length from text field (may be "50" or "50ft" etc.) ──────
        length_ft = None
        if length_ft_raw:
            m = re.search(r"(\d+(?:\.\d+)?)", length_ft_raw)
            if m:
                length_ft = float(m.group(1))

        # ── Derive best factsheet URL ────────────────────────────────────────
        best_url = factsheet_url or website_url or None

        # ── Use portfolio field, fall back to name ───────────────────────────
        portfolio_key = portfolio or name

        # ── Slug for image lookup ────────────────────────────────────────────
        slug = to_slug(name)

        record = {
            "_id":                  item["id"],   # internal — used for dedup, stripped later
            "name":                 name,
            "designNumber":         design_number or None,
            "portfolio":            portfolio_key,
            "type":                 item_type or None,
            "style":                norm_style,
            "rawStyle":             raw_style or None,    # original Monday value
            "grossSF":              int(gross_sf) if gross_sf else None,
            "livableSF":            int(livable_sf) if livable_sf else None,
            "widthFt":              int(width_ft) if width_ft else None,
            "lengthFt":             int(length_ft) if length_ft else None,
            "bedrooms":             int(bedrooms) if bedrooms else None,
            "bathrooms":            bathrooms,     # keep as float for 1.5, 2.5 etc.
            "status":               status or None,
            "jurisdiction":         norm_juris,
            "rawJurisdiction":      raw_juris or None,    # original Monday value
            "factsheetUrl":         best_url,
            "licenseFee":           license_fee or None,
            "ahjDate":              ahj_date or None,
            "footprintNote":        footprint_note or None,
            "slug":                 slug,
            "primaryVariant":       False,         # set in second pass
            "scoreable":            False,         # set in second pass
        }

        results.append(record)

        # Group by portfolio for primary variant selection
        if portfolio_key not in items_by_portfolio:
            items_by_portfolio[portfolio_key] = []
        items_by_portfolio[portfolio_key].append(record)

    # ── Second pass: mark primary variants ──────────────────────────────────
    primary_ids = determine_primary_variant(items_by_portfolio)
    for record in results:
        if record["_id"] in primary_ids:
            record["primaryVariant"] = True
            # Scoreable = primary variant of a Main type plan with enough data to score.
            # Status filter above already guarantees Approved or Plan Check — no need
            # to re-check status here.
            is_main  = (record.get("type") or "").lower() == "main"
            has_data = bool(record.get("bedrooms") or record.get("grossSF"))
            record["scoreable"] = bool(is_main and has_data)

    # ── Strip internal _id field before export ───────────────────────────────
    for record in results:
        del record["_id"]

    print(f"   ✓ {len(results)} plans included (Approved + Plan Check)")
    print(f"   ✓ {excluded_count} plans excluded (CD / SD / In Development / blank)")
    primary_count   = sum(1 for r in results if r["primaryVariant"])
    scoreable_count = sum(1 for r in results if r["scoreable"])
    print(f"   ✓ {primary_count} primary variants identified")
    print(f"   ✓ {scoreable_count} scoreable plans")

    return results


def sync_catalog_to_github(catalog: list) -> None:
    """Commit catalog.json to GitHub. Only writes if content has changed."""
    content = json.dumps(catalog, indent=2, ensure_ascii=False)
    content_bytes = content.encode("utf-8")

    # Compute git blob SHA to compare with existing file
    new_sha = hashlib.sha1(
        f"blob {len(content_bytes)}\0".encode() + content_bytes
    ).hexdigest()

    print(f"\n🐙  Connecting to GitHub repo: {GITHUB_REPO}")
    gh   = Github(GITHUB_TOKEN)
    repo = gh.get_repo(GITHUB_REPO)

    try:
        existing = repo.get_contents(OUTPUT_PATH)
        if existing.sha == new_sha:
            print(f"   ✓ {OUTPUT_PATH} unchanged — no commit needed")
            return
        repo.update_file(
            OUTPUT_PATH,
            f"Sync catalog.json — {len(catalog)} plans",
            content_bytes,
            existing.sha,
            branch="main",
        )
        print(f"   ↑ Updated {OUTPUT_PATH} ({len(catalog)} plans)")
    except GithubException as e:
        if e.status == 404:
            repo.create_file(
                OUTPUT_PATH,
                f"Add catalog.json — {len(catalog)} plans",
                content_bytes,
                branch="main",
            )
            print(f"   + Created {OUTPUT_PATH} ({len(catalog)} plans)")
        else:
            raise


def main():
    validate_env()
    raw_items = get_catalog_items()
    catalog   = transform_items(raw_items)

    if not catalog:
        print("⚠️  No catalog items to sync.")
        sys.exit(0)

    sync_catalog_to_github(catalog)
    print("\n✅  Catalog sync complete")


if __name__ == "__main__":
    main()
