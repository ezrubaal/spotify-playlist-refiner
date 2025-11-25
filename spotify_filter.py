#!/usr/bin/env python3

import sys
import re
import json
from collections import defaultdict
from pathlib import Path

import spotipy
from spotipy.oauth2 import SpotifyOAuth

# Default cutoff year – you will be asked and can override this
DEFAULT_CUTOFF_YEAR = 1992

# Duration threshold for automatic duplicate cleanup (in seconds)
DUP_DURATION_THRESHOLD_SEC = 3

# Scopes: read playlist + modify it
SCOPE = (
    "playlist-read-private "
    "playlist-read-collaborative "
    "playlist-modify-public "
    "playlist-modify-private"
)

# Simple JSON file to remember which tracks you chose to keep (by track ID)
CACHE_PATH = Path("playlist_refiner_cache.json")


# ---------- Cache helpers ----------

def load_decision_cache():
    if CACHE_PATH.is_file():
        try:
            with CACHE_PATH.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {"keep": []}
            data.setdefault("keep", [])
            return data
        except Exception:
            return {"keep": []}
    return {"keep": []}


def save_decision_cache(cache):
    try:
        with CACHE_PATH.open("w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        print(f"Warning: could not save cache file: {e}")


# ---------- Spotify helpers ----------

def extract_playlist_id(s: str) -> str:
    """Accepts a bare ID or a full URL and returns the playlist ID."""
    s = s.strip()
    if "playlist/" in s:
        return s.split("playlist/")[1].split("?")[0]
    return s.split("?")[0]


def get_all_tracks(sp, playlist_id):
    """Fetch all tracks from a playlist, handling pagination."""
    items = []
    results = sp.playlist_items(
        playlist_id,
        additional_types=["track"],
        limit=100,
    )
    items.extend(results["items"])
    while results["next"]:
        results = sp.next(results)
        items.extend(results["items"])
    return items


def get_all_playlists(sp):
    """Fetch all playlists for current user."""
    playlists = []
    results = sp.current_user_playlists(limit=50)
    playlists.extend(results["items"])
    while results["next"]:
        results = sp.next(results)
        playlists.extend(results["items"])
    return playlists


def choose_playlist_interactively(sp):
    """List user-owned playlists and let user choose by number."""
    me = sp.current_user()
    my_id = me["id"]
    playlists = get_all_playlists(sp)

    owned = [pl for pl in playlists if pl["owner"]["id"] == my_id]

    if not owned:
        print("No playlists owned by this account were found.")
        return None

    print("\nYour playlists:")
    for idx, pl in enumerate(owned, start=1):
        name = pl["name"]
        tracks_total = pl["tracks"]["total"]
        public_flag = "public" if pl.get("public") else "private"
        print(f"{idx:2d}. {name} ({tracks_total} tracks, {public_flag})")

    while True:
        choice = input(
            "\nEnter the playlist number to edit or 'q' to quit: "
        ).strip().lower()
        if choice == "q":
            return None
        if not choice.isdigit():
            print("Please enter a valid number.")
            continue
        num = int(choice)
        if not (1 <= num <= len(owned)):
            print("Number out of range.")
            continue
        return owned[num - 1]["id"]


# ---------- Normalization helpers ----------

def normalize_title(name: str) -> str:
    """
    Normalize track title for duplicate grouping:
    - lowercase
    - unify dashes
    - strip common 'remaster / reissue / version / edit / mix' qualifiers
    - clean extra spaces
    """
    name = name.lower()
    name = name.replace("–", "-").replace("—", "-")
    name = re.sub(r"\s+", " ", name).strip()

    qualifier_words = (
        r"remaster(ed)?|reissue|version|edit|mix|remix|mono|stereo|single|"
        r"radio edit|album version"
    )

    # (Remastered), (2025 Reissue), etc.
    name = re.sub(rf"\s*\(([^)]*({qualifier_words})[^)]*)\)", "", name)
    # [Remastered], [2025 Version], etc.
    name = re.sub(rf"\s*\[([^\]]*({qualifier_words})[^\]]*)\]", "", name)
    # " - Remastered", " - 2011 Remaster", " - 2025 version"
    name = re.sub(rf"\s*-\s*(\d{{4}}\s*)?({qualifier_words})\b.*$", "", name)
    # bare " - 2025"
    name = re.sub(r"\s*-\s*\d{4}\b$", "", name)

    name = re.sub(r"\s+", " ", name).strip(" -")
    return name


def normalize_artist(name: str) -> str:
    """Normalize main artist name for duplicate grouping."""
    return name.lower().strip()


# ---------- Duplicates commit helper ----------

def commit_duplicate_removals(sp, playlist_id, dup_removals, ask_confirm=True):
    """
    Remove specific occurrences of duplicate tracks.
    dup_removals entries must have:
      - "track_id": Spotify track ID (not full URI)
      - "position": playlist index (0-based)

    Returns True if something was actually removed.
    """
    if not dup_removals:
        print("No duplicates selected for removal.")
        return False

    # Filter & group by track_id
    items_map = defaultdict(list)
    skipped = 0

    for e in dup_removals:
        track_id = e.get("track_id")
        pos = e.get("position")
        if track_id is None:
            skipped += 1
            continue
        items_map[track_id].append(pos)

    if not items_map:
        print("Nothing valid to remove (all candidates had no track_id).")
        return False

    if skipped:
        print(f"(Skipped {skipped} entries without a valid track_id – likely local/unsupported tracks.)")

    occurrences_count = sum(len(v) for v in items_map.values())
    print(f"\nDuplicate occurrences selected for removal: {occurrences_count}")

    if ask_confirm:
        confirm = input(
            "Really remove these duplicate occurrences? [y/N] "
        ).strip().lower()
        if confirm != "y":
            print("Duplicate removal aborted.")
            return False

    # Build payload and send in batches of <= 100 items
    items_payload = [
        {"uri": track_id, "positions": positions}
        for track_id, positions in items_map.items()
    ]

    max_per_request = 100
    total_items = len(items_payload)
    num_batches = (total_items + max_per_request - 1) // max_per_request

    print(f"Removing {total_items} track IDs in {num_batches} batch(es)...")

    try:
        for start in range(0, total_items, max_per_request):
            batch = items_payload[start:start + max_per_request]
            sp.playlist_remove_specific_occurrences_of_items(playlist_id, batch)
        print("Duplicate occurrences removed.\n")
        return True
    except spotipy.SpotifyException as e:
        print("Error while removing duplicates:", e)
        return False


# ---------- Automatic duplicate cleanup (duration-based) ----------

def auto_duplicates_step(sp, playlist_id, tracks, keep_set):
    """
    Optional automatic duplicate cleanup:
    - Groups by normalized title + main artist.
    - Within each group, uses the earliest occurrence as base.
    - Marks later entries as auto-duplicates if duration difference <= threshold.
    - Shows a summary list and lets the user:
        y  -> delete them all (keep first instance in group)
        n  -> skip auto cleanup, go to manual
        e  -> exclude specific rows from the auto-delete list
    """
    threshold_ms = DUP_DURATION_THRESHOLD_SEC * 1000

    groups = defaultdict(list)

    for index, item in enumerate(tracks):
        track = item["track"]
        if track is None:
            continue

        title = track["name"]
        track_id = track["id"]
        artists_list = [a["name"] for a in track["artists"]]
        main_artist = artists_list[0] if artists_list else "Unknown"

        key = (normalize_title(title), normalize_artist(main_artist))

        album = track["album"]
        album_name = album["name"]
        release_date = album.get("release_date") or "unknown"
        if release_date and release_date[:4].isdigit():
            year_str = release_date[:4]
        else:
            year_str = "????"

        dur_ms = track.get("duration_ms") or 0
        total_sec = dur_ms // 1000
        mins = total_sec // 60
        secs = total_sec % 60
        duration_str = f"{mins}:{secs:02d}"

        groups[key].append(
            {
                "playlist_index": index,
                "title": title,
                "artists": artists_list,
                "album_name": album_name,
                "release_date": release_date,
                "year_str": year_str,
                "uri": track["uri"],
                "duration_ms": dur_ms,
                "duration_str": duration_str,
                "track_id": track_id,
            }
        )

    # Build list of auto-candidates: entries to delete, with their base track
    auto_candidates = []

    for key, entries in groups.items():
        if len(entries) < 2:
            continue

        sorted_entries = sorted(entries, key=lambda e: e["playlist_index"])
        base = sorted_entries[0]
        base_dur = base["duration_ms"]

        if base_dur <= 0:
            continue

        for e in sorted_entries[1:]:
            if e["duration_ms"] <= 0:
                continue
            if abs(e["duration_ms"] - base_dur) <= threshold_ms:
                auto_candidates.append({"entry": e, "base": base})

    if not auto_candidates:
        print(
            f"No candidates found for automatic duplicate cleanup "
            f"within ±{DUP_DURATION_THRESHOLD_SEC} seconds.\n"
        )
        return False  # playlist not changed

    # Compute column widths so columns line up nicely
    col1_w = 0  # title – artists
    col2_w = 0  # album
    col3_w = 0  # release date
    col4_w = 0  # duration

    for c in auto_candidates:
        e = c["entry"]
        b = c["base"]

        cand_artists = ", ".join(e["artists"])
        base_artists = ", ".join(b["artists"])

        cand_col1 = f"{e['title']} – {cand_artists}"
        base_col1 = f"{b['title']} – {base_artists}"

        col1_w = max(col1_w, len(cand_col1), len(base_col1))
        col2_w = max(col2_w, len(e["album_name"]), len(b["album_name"]))
        col3_w = max(col3_w, len(e["release_date"]), len(b["release_date"]))
        col4_w = max(col4_w, len(e["duration_str"]), len(b["duration_str"]))


    # Show summary
    print(
        f"\n=== Automatic duplicate cleanup suggestion "
        f"(threshold: ±{DUP_DURATION_THRESHOLD_SEC} seconds) ==="
    )
    print(
        "The following tracks look like duplicates (same song/artist, similar length).\n"
        "For each pair, the FIRST line is the one that would be DELETED, and\n"
        "the 'kept as:' line shows the track that will be KEPT.\n"
    )

    for idx, c in enumerate(auto_candidates, start=1):
        e = c["entry"]
        b = c["base"]  # base track to keep

        cand_artists = ", ".join(e["artists"])
        base_artists = ", ".join(b["artists"])

        # Build padded columns for the candidate
        cand_col1 = f"{e['title']} – {cand_artists}".ljust(col1_w)
        cand_col2 = e["album_name"].ljust(col2_w)
        cand_col3 = e["release_date"].ljust(col3_w)
        cand_col4 = e["duration_str"].ljust(col4_w)

        # Build padded columns for the base (kept) track
        base_col1 = f"{b['title']} – {base_artists}".ljust(col1_w)
        base_col2 = b["album_name"].ljust(col2_w)
        base_col3 = b["release_date"].ljust(col3_w)
        base_col4 = b["duration_str"].ljust(col4_w)

        print(
            f"{idx:3d}. original: {cand_col1} | {cand_col2} | {cand_col3} | "
            f"{cand_col4} (playlist position {e['playlist_index']+1})"
        )
        print(
            f"     kept as: {base_col1} | {base_col2} | {base_col3} | "
            f"{base_col4} (playlist position {b['playlist_index']+1})"
        )


    # Ask user what to do
    while True:
        resp = input(
            "\nApply this automatic cleanup? "
            "[y = yes, n = no, e = exclude some rows] "
        ).strip().lower()
        if resp in ("y", "n", "e", ""):
            break

    if resp in ("", "n"):
        print("Skipping automatic duplicate cleanup.\n")
        return False

    final_candidates = auto_candidates

    if resp == "e":
        while True:
            exclude_str = input(
                "Enter row numbers to EXCLUDE from auto deletion "
                "(e.g. '2' or '2,5,10'), or press Enter to delete all: "
            ).strip().lower()

            if not exclude_str:
                exclude_set = set()
                break

            try:
                nums = [
                    int(x) for x in exclude_str.replace(" ", "").split(",") if x
                ]
                if not nums:
                    exclude_set = set()
                    break
                if not all(1 <= n <= len(auto_candidates) for n in nums):
                    raise ValueError
                exclude_set = set(nums)
                break
            except ValueError:
                print("Invalid input. Please enter valid row numbers from the list.")

        if exclude_set:
            final_candidates = [
                c for idx, c in enumerate(auto_candidates, start=1)
                if idx not in exclude_set
            ]
        if not final_candidates:
            print("No tracks left for automatic deletion. Skipping.\n")
            return False

    # Prepare deletions and mark base tracks as 'kept'
    dup_removals = []
    base_ids_to_keep = set()

    for c in final_candidates:
        e = c["entry"]
        base = c["base"]
        dup_removals.append(
            {"track_id": e["track_id"], "position": e["playlist_index"]}
        )
        if base["track_id"]:
            base_ids_to_keep.add(base["track_id"])

    # Actually delete (no extra confirm here; we already asked)
    changed = commit_duplicate_removals(
        sp, playlist_id, dup_removals, ask_confirm=False
    )
    if changed:
        for tid in base_ids_to_keep:
            keep_set.add(tid)
    return changed


# ---------- Manual duplicate review ----------

def manual_duplicates_step(sp, playlist_id, tracks, keep_set):
    """
    Manual duplicate review step.
    Behavior:
    - For each group of potential duplicates (same normalized title + main artist):
      * Enter numbers to KEEP (e.g. '2' or '1,3') -> remove all others.
      * Enter '-2,3' to REMOVE those entries and keep the others.
      * Enter nothing -> keep all.
      * 'q' -> stop duplicate review and apply removals so far.
    """
    groups = defaultdict(list)

    for index, item in enumerate(tracks):
        track = item["track"]
        if track is None:
            continue

        title = track["name"]
        track_id = track["id"]
        artists_list = [a["name"] for a in track["artists"]]
        main_artist = artists_list[0] if artists_list else "Unknown"
        key = (normalize_title(title), normalize_artist(main_artist))

        album = track["album"]
        album_name = album["name"]
        release_date = album.get("release_date") or "unknown"
        if release_date and release_date[:4].isdigit():
            year_str = release_date[:4]
        else:
            year_str = "????"

        dur_ms = track.get("duration_ms") or 0
        total_sec = dur_ms // 1000
        mins = total_sec // 60
        secs = total_sec % 60
        duration_str = f"{mins}:{secs:02d}"

        groups[key].append(
            {
                "playlist_index": index,  # 0-based index in playlist
                "title": title,
                "artists": artists_list,
                "album_name": album_name,
                "release_date": release_date,
                "year_str": year_str,
                "uri": track["uri"],
                "duration_ms": dur_ms,
                "duration_str": duration_str,
                "track_id": track_id,
            }
        )

    dup_groups = {k: v for k, v in groups.items() if len(v) > 1}
    if not dup_groups:
        print("No obvious duplicates (same song title + main artist).")
        return

    dup_removals = []

    print("\n=== Manual duplicate review ===")
    print(
        "For each group, you’ll see multiple copies/versions of the same song.\n"
        "- Enter numbers to KEEP (e.g. '2' or '1,3') -> remove all others.\n"
        "- Enter '-2,3' to REMOVE those entries and keep the others.\n"
        "- Press Enter to keep all.\n"
        "- Type 'q' to stop duplicate review and move on.\n"
    )

    for (title_norm, artist_norm), entries in dup_groups.items():
        rep_title = entries[0]["title"]
        rep_artist = entries[0]["artists"][0] if entries[0]["artists"] else "Unknown"
        print("-" * 70)
        print(f"Possible duplicates for: {rep_title} – {rep_artist}")
        for i, e in enumerate(entries, start=1):
            artists_str = ", ".join(e["artists"])
            print(f" {i}) playlist position {e['playlist_index']+1}")
            print(f"    {e['title']} – {artists_str}")
            print(f"    album: {e['album_name']}")
            print(f"    release: {e['release_date']} (year {e['year_str']})")
            print(f"    duration: {e['duration_str']}")

        while True:
            resp = input(
                "Numbers to KEEP (e.g. '2' or '1,3'), "
                "or '-2,3' to REMOVE those, Enter = keep all, 'q' = quit: "
            ).strip().lower()

            if resp == "q":
                commit_duplicate_removals(sp, playlist_id, dup_removals, ask_confirm=True)
                return

            if not resp:
                # keep all entries in this group, and remember them as kept
                for e in entries:
                    if e["track_id"]:
                        keep_set.add(e["track_id"])
                break

            # detect remove mode if response starts with '-'
            remove_mode = False
            s = resp
            if s.startswith("-"):
                remove_mode = True
                s = s[1:].strip()

            try:
                nums = [int(x) for x in s.replace(" ", "").split(",") if x]
                if not nums:
                    for e in entries:
                        if e["track_id"]:
                            keep_set.add(e["track_id"])
                    break
                if not all(1 <= n <= len(entries) for n in nums):
                    raise ValueError
            except ValueError:
                print("Invalid input. Please enter valid numbers from the list.")
                continue

            selected = set(nums)

            if remove_mode:
                # REMOVE only the selected entries, keep others
                for idx_e, e in enumerate(entries, start=1):
                    if idx_e in selected:
                        dup_removals.append(
                            {"track_id": e["track_id"], "position": e["playlist_index"]}
                        )
                    else:
                        if e["track_id"]:
                            keep_set.add(e["track_id"])
            else:
                # KEEP only the selected entries -> remove all others
                for idx_e, e in enumerate(entries, start=1):
                    if idx_e in selected:
                        if e["track_id"]:
                            keep_set.add(e["track_id"])
                    else:
                        dup_removals.append(
                            {"track_id": e["track_id"], "position": e["playlist_index"]}
                        )

            break

    commit_duplicate_removals(sp, playlist_id, dup_removals, ask_confirm=True)


def handle_duplicates(sp, playlist_id, tracks, keep_set):
    """
    Orchestrates duplicate handling:
    1. Optional automatic duration-based cleanup.
    2. Then manual duplicate review on the updated playlist.
    """
    # Step 1: optional automatic cleanup
    changed = auto_duplicates_step(sp, playlist_id, tracks, keep_set)

    # Step 2: manual review, possibly on updated playlist
    if changed:
        tracks = get_all_tracks(sp, playlist_id)
    manual_duplicates_step(sp, playlist_id, tracks, keep_set)


# ---------- Year-based filtering ----------

def review_tracks_by_year(sp, playlist_id, tracks, cutoff_year, keep_set):
    print(
        f"\n=== Year-based cleanup (albums after {cutoff_year}) ===\n"
        f"Only tracks whose album year is > {cutoff_year} (or unknown) will be shown.\n"
    )

    to_remove_uris = []

    for idx, item in enumerate(tracks, start=1):
        track = item["track"]
        if track is None:
            continue

        track_id = track["id"]
        # Skip tracks we already decided to keep in a previous run
        if track_id in keep_set:
            continue

        track_name = track["name"]
        artists = ", ".join(a["name"] for a in track["artists"])
        album = track["album"]
        album_name = album["name"]
        release_date = album.get("release_date") or "unknown"
        precision = album.get("release_date_precision", "day")

        if release_date and release_date[:4].isdigit():
            year_str = release_date[:4]
        else:
            year_str = "????"

        # Auto-keep albums clearly released on/before the cutoff year
        if year_str != "????" and int(year_str) <= cutoff_year:
            continue

        print("-" * 70)
        print(f"{idx}. {track_name} – {artists}")
        print(f"   Album: {album_name}")
        print(
            f"   Spotify album release_date: {release_date} "
            f"(precision: {precision})"
        )
        print(f"   -> Album year considered: {year_str}")
        print(
            "   (If this is a re-release but the *real* album is pre-"
            f"{cutoff_year+1}, you can choose to keep it.)"
        )

        while True:
            choice = input(
                "Delete this track from the playlist? "
                "[y = delete, n = keep, q = quit] "
            ).strip().lower()
            if choice in ("y", "n", "q", ""):
                break

        if choice == "q":
            print("Stopping year-based review.")
            break
        elif choice == "y":
            to_remove_uris.append(track["uri"])
        else:
            # default / 'n' / ''  -> keep track and remember that decision
            if track_id:
                keep_set.add(track_id)
            continue

    # Deduplicate URIs (just in case)
    to_remove_uris = list(dict.fromkeys(to_remove_uris))

    print("\nSummary (year-based):")
    print(f"Tracks marked for removal: {len(to_remove_uris)}")

    if not to_remove_uris:
        print("Nothing to remove based on year.")
        return

    confirm = input(
        "Really remove these tracks from the playlist? [y/N] "
    ).strip().lower()
    if confirm != "y":
        print("Year-based removal aborted.")
        return

    # Spotify API lets you remove up to 100 items per call
    for i in range(0, len(to_remove_uris), 100):
        batch = to_remove_uris[i : i + 100]
        sp.playlist_remove_all_occurrences_of_items(playlist_id, batch)

    print("Done. Year-based removals applied.")


def ask_cutoff_year():
    """Ask user for cutoff year, with validation & default."""
    while True:
        s = input(
            f"Cutoff year? "
            f"(tracks with album year > this will be reviewed; default {DEFAULT_CUTOFF_YEAR}): "
        ).strip()
        if not s:
            return DEFAULT_CUTOFF_YEAR
        if not s.isdigit():
            print("Please enter a valid year (e.g. 1992) or leave empty for default.")
            continue
        year = int(s)
        if year < 1900 or year > 2100:
            print("That year looks suspicious; please enter something between 1900 and 2100.")
            continue
        return year


# ---------- Main ----------

def main():
    # Uses environment variables by default; or you can pass client_id etc
    sp = spotipy.Spotify(auth_manager=SpotifyOAuth(scope=SCOPE))

    me = sp.current_user()
    print(f"Logged in as: {me['display_name']} ({me['id']})")

    # If user gave playlist URL/ID on the command line, use it.
    # Otherwise show a menu of their playlists.
    if len(sys.argv) > 1:
        playlist_input = sys.argv[1]
        playlist_id = extract_playlist_id(playlist_input)
    else:
        playlist_id = choose_playlist_interactively(sp)
        if playlist_id is None:
            print("No playlist selected, exiting.")
            return

    playlist = sp.playlist(playlist_id, fields="name,owner(display_name),id")
    print(
        f"\nLoaded playlist: {playlist['name']} "
        f"(owner: {playlist['owner']['display_name']})"
    )

    # Load cache of previous "keep" decisions
    decision_cache = load_decision_cache()
    keep_set = set(decision_cache.get("keep", []))

    cutoff_year = ask_cutoff_year()
    print(f"Using cutoff year: {cutoff_year}\n")

    tracks = get_all_tracks(sp, playlist_id)
    print(f"Found {len(tracks)} tracks.\n")

    # Step 1: duplicates (auto + manual)
    handle_duplicates(sp, playlist_id, tracks, keep_set)

    # Step 2: re-fetch playlist and do year-based cleanup
    tracks = get_all_tracks(sp, playlist_id)
    review_tracks_by_year(sp, playlist_id, tracks, cutoff_year, keep_set)

    # Save updated keep_set back to cache
    decision_cache["keep"] = sorted(keep_set)
    save_decision_cache(decision_cache)


if __name__ == "__main__":
    main()