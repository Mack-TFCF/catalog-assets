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
  name                    → name
  text_mkvnr32v           → designNumber
  text_mm0pe0s0           → countyPlanNumber   (LA County Standard Plan #, e.g. "25-01")
  text_mm0p5s7r           → bsPlanNumber       (B&S floor plan identity key)
  text_mkvbtdj0           → rpplNumber         (RPPL #)
  text_mkvb7334           → bldrNumber         (Permit Number - BLDR #)
  color_mm0q2xxk          → type               (status: Main / ADU / Garage)
  color_mkyrtwgf          → currentPhase       (AHJ - Approved / PC - Plan Check / CD / SD / etc.)
  color_mkyht2q7          → jurisdiction       (raw Monday value: "LA County", "LA City")
  color_mkyhbd15          → codeCycle          (Code Cycle - Building)
  color_mkzvv8jk          → approvedStatus     (status)
  numeric_mkvbbxbv        → grossSF
  numeric_mm0q2d4e        → livableSF
  numeric_mm1bxp3p        → widthFt
  numeric_mkvbkb7c        → bedrooms
  numeric_mkvb2bw7        → bathrooms
  numeric_mkyaz2b4        → licenseFee         (numeric field)
  dropdown_mkvbac06       → permittingJurisdiction
  dropdown_mkvnkg2h       → style
  link_mkvcth2r           → factsheetUrl       (link field)
  link_mm3a1xdk           → websiteUrl         (link field — stored separately from factsheetUrl)
  date_mkzvxjd3           → ahjDate
  text_mkvgnxaj           → lengthFt           (text field — cast to float)
  text_mkvbd7bd           → footprintNote      (free text, e.g. "30x50")
  board_relation_mkwb8d4b → portfolio          (relation — use linked item name)
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

# Columns to fetch (all catalog fields — no filtering in sync layer)
COLUMN_IDS = [
    "text_mkvnr32v",           # Design Number
    "text_mm0pe0s0",           # County Plan Number (LA Standard Plan #)
    "text_mm0p5s7r",           # B&S Standard Plan # — floor plan identity key
    "text_mkvbtdj0",           # RPPL #
    "text_mkvb7334",           # Permit Number - BLDR # — structural variant identity
    "color_mm0q2xxk",          # Type (Main / ADU / Garage)
    "color_mkyrtwgf",          # Current Phase (AHJ - Approved / PC - Plan Check / etc.)
    "color_mkyht2q7",          # Jurisdiction (raw: LA County / LA City)
    "color_mkyhbd15",          # Code Cycle (Building)
    "color_mkzvv8jk",          # Approved status
    "numeric_mkvbbxbv",        # Gross SF
    "numeric_mm0q2d4e",        # Livable SF
    "numeric_mm1bxp3p",        # Width (Ft)
    "numeric_mkvbkb7c",        # Bedrooms
    "numeric_mkvb2bw7",        # Bathrooms
    "numeric_mkyaz2b4",        # Licence Fee (numeric)
    "dropdown_mkvbac06",       # Permitting Jurisdiction
    "dropdown_mkvnkg2h",       # Style
    "link_mkvcth2r",           # Current Factsheet Link
    "link_mm3a1xdk",           # Website URL
    "date_mkzvxjd3",           # AHJ Date
    "text_mkvgnxaj",           # Length (Ft) — text field, cast to float
    "text_mkvbd7bd",           # Footprint (free text)
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


# Finder-visible Current Phase values. The export includes the full catalog
# lifecycle, but Finder recommendation scoring should still use this subset.
ACTIVE_STATUSES = {"AHJ - Approved", "PC - Plan Check"}

NULL_PLAN_NUMBERS = {"", "TBD", "N/A", "NA"}


def normalize_bs_plan_number(value: str) -> str | None:
    """Normalize non-joinable B&S plan numbers to null for analytics."""
    text = (value or "").strip()
    if text.upper() in NULL_PLAN_NUMBERS:
        return None
    return text


def derive_phase_group(current_phase: str) -> str:
    """Map Monday Current Phase values to Intelligence lifecycle buckets."""
    phase = (current_phase or "").strip()
    if not phase:
        return "unknown"

    normalized = re.sub(r"\s+", " ", phase).strip().lower()
    prefix_match = re.match(r"^([a-z]{2,4})(?:\b|\s*-)", normalized)
    phase_code = prefix_match.group(1).upper() if prefix_match else ""

    if phase_code == "AHJ" and "approved" in normalized:
        return "approved"
    if phase_code in {"PC", "AHJ"}:
        return "in_review"
    if phase_code == "CD":
        return "developed"
    if phase_code in {"CP", "SD", "DD", "ZC"}:
        return "development"

    if "approved" in normalized or "pre-approved" in normalized:
        return "approved"
    if "construction documentation" in normalized or "construction documents" in normalized:
        return "developed"
    if "zoning" in normalized or "schematic" in normalized or "design development" in normalized:
        return "development"
    if "plan check" in normalized or "review" in normalized:
        return "in_review"

    return "unknown"


def transform_items(raw_items: list) -> list:
    """Transform raw Monday items into the catalog JSON schema."""
    results = []
    items_by_portfolio = {}
    active_items_by_portfolio = {}
    skipped_count = 0

    for item in raw_items:
        name = item["name"].strip()
        if not name or name.lower() == "none":
            continue

        # Skip obvious test/placeholder entries
        name_lower = name.lower()
        if (name_lower.startswith("test") or
                name_lower.startswith("placeholder") or
                name_lower.startswith("do not use") or
                name_lower == "none"):
            skipped_count += 1
            continue

        cv = item["column_values"]

        # ── Extract all fields ──────────────────────────────────────────────
        design_number      = extract_col(cv, "text_mkvnr32v")
        county_plan_number = extract_col(cv, "text_mm0pe0s0")       # LA County Standard Plan #
        raw_bs_plan_number = extract_col(cv, "text_mm0p5s7r")       # B&S Standard Plan # — floor plan identity
        bs_plan_number     = normalize_bs_plan_number(raw_bs_plan_number)
        rppl_number        = extract_col(cv, "text_mkvbtdj0")       # RPPL #
        bldr_number        = extract_col(cv, "text_mkvb7334")       # Permit Number - BLDR #
        item_type          = extract_col(cv, "color_mm0q2xxk")      # Type (Main / ADU / Garage)
        current_phase      = extract_col(cv, "color_mkyrtwgf")      # Current Phase
        code_cycle         = extract_col(cv, "color_mkyhbd15")      # Code Cycle (Building)
        gross_sf           = extract_number(cv, "numeric_mkvbbxbv")
        livable_sf         = extract_number(cv, "numeric_mm0q2d4e")
        width_ft           = extract_number(cv, "numeric_mm1bxp3p")
        length_ft_raw      = extract_col(cv, "text_mkvgnxaj")       # text field — cast to float
        bedrooms           = extract_number(cv, "numeric_mkvbkb7c")
        bathrooms          = extract_number(cv, "numeric_mkvb2bw7")
        # ── Normalise style; jurisdiction stored as raw Monday value ─────────
        raw_style    = extract_col(cv, "dropdown_mkvnkg2h")
        norm_style   = STYLE_MAP.get(raw_style, raw_style) if raw_style else None

        raw_juris    = extract_col(cv, "color_mkyht2q7")
        perm_juris   = extract_col(cv, "dropdown_mkvbac06")
        factsheet_url   = extract_link(cv, "link_mkvcth2r")
        website_url     = extract_link(cv, "link_mm3a1xdk")
        license_fee     = extract_number(cv, "numeric_mkyaz2b4")
        approved_status = extract_col(cv, "color_mkzvv8jk")
        ahj_date        = extract_col(cv, "date_mkzvxjd3")
        footprint_note  = extract_col(cv, "text_mkvbd7bd")
        portfolio       = extract_portfolio_name(cv, "board_relation_mkwb8d4b")

        # ── Derive length from text field (may be "50" or "50ft" etc.) ──────
        length_ft = None
        if length_ft_raw:
            m = re.search(r"(\d+(?:\.\d+)?)", length_ft_raw)
            if m:
                length_ft = float(m.group(1))

        # ── Derive base portfolio name — strip structural variant suffixes ────
        # "(Vault)", "(Truss)", "(Attached Garage)", "(Two Story)" etc. are
        # structural variants of the same design. Strip them to get the canonical
        # portfolio group name used for Problem 1 deduplication.
        import re as _re
        base_portfolio = _re.sub(
            r'\s*\((vault|truss|attached garage|two story|detached garage)\)\s*$',
            '', (portfolio or name), flags=_re.IGNORECASE
        ).strip()

        # Use base portfolio for grouping (deduplicates Vault/Truss variants)
        # Fall back to full name if no portfolio relation set
        portfolio_key = base_portfolio or name

        # ── Slug for image lookup ────────────────────────────────────────────
        slug = to_slug(name)

        record = {
            "_id":                  item["id"],   # internal — used for dedup, stripped later
            "mondayItemId":         item["id"],   # stable row identifier for Intelligence
            "name":                 name,
            "designNumber":         design_number or None,
            "countyPlanNumber":     county_plan_number or None,
            "bsPlanNumber":         bs_plan_number,          # normalized floor plan identity
            "rawBsPlanNumber":      raw_bs_plan_number or None,
            "rpplNumber":           rppl_number or None,
            "bldrNumber":           bldr_number or None,     # structural permit number
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
            "status":               current_phase or None,   # temporary Finder compatibility alias
            "currentPhase":         current_phase or None,
            "codeCycle":            code_cycle or None,
            "phaseGroup":           derive_phase_group(current_phase),
            "jurisdiction":         raw_juris or perm_juris or None,
            "rawJurisdiction":      raw_juris or None,    # original Monday value
            "factsheetUrl":         factsheet_url or None,
            "websiteUrl":           website_url or None,
            "licenseFee":           license_fee,
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
        if current_phase in ACTIVE_STATUSES:
            if portfolio_key not in active_items_by_portfolio:
                active_items_by_portfolio[portfolio_key] = []
            active_items_by_portfolio[portfolio_key].append(record)

    # ── Second pass: mark primary variants ──────────────────────────────────
    primary_source_by_portfolio = {}
    for portfolio, items in items_by_portfolio.items():
        primary_source_by_portfolio[portfolio] = active_items_by_portfolio.get(portfolio) or items

    primary_ids = determine_primary_variant(primary_source_by_portfolio)
    for record in results:
        if record["_id"] in primary_ids:
            record["primaryVariant"] = True
            # Scoreable = primary variant of a Main type plan with enough data to score.
            # The export now includes all phases, so keep Finder scoring limited
            # to Approved and Plan Check records.
            is_main  = (record.get("type") or "").lower() == "main"
            has_data = bool(record.get("bedrooms") or record.get("grossSF"))
            is_active = record.get("currentPhase") in ACTIVE_STATUSES
            record["scoreable"] = bool(is_active and is_main and has_data)

    # ── Strip internal _id field before export ───────────────────────────────
    for record in results:
        del record["_id"]

    phase_counts = {}
    group_counts = {}
    for record in results:
        phase = record.get("currentPhase") or "(blank)"
        group = record.get("phaseGroup") or "unknown"
        phase_counts[phase] = phase_counts.get(phase, 0) + 1
        group_counts[group] = group_counts.get(group, 0) + 1

    print(f"   ✓ {len(results)} plans included (all Current Phase values)")
    print(f"   ✓ {skipped_count} placeholder/test plans skipped")
    print(f"   ✓ Current Phase values: {phase_counts}")
    print(f"   ✓ Phase groups: {group_counts}")
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
