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

# Scopes: read playlist + modify it
SCOPE = (
    "playlist-read-private "
    "playlist-read-collaborative "
    "playlist-modify-public "
    "playlist-modify-private"
)

# Simple JSON file to remember which tracks you chose to keep
CACHE_PATH = Path("playlist_refiner_cache.json")


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


def commit_duplicate_removals(sp, playlist_id, dup_removals):
    if not dup_removals:
        print("No duplicates selected for removal.")
        return

    print(f"\nDuplicate occurrences selected for removal: {len(dup_removals)}")
    confirm = input(
        "Really remove these duplicate occurrences? [y/N] "
    ).strip().lower()
    if confirm != "y":
        print("Duplicate removal aborted.")
        return

    items_map = defaultdict(list)
    for e in dup_removals:
        items_map[e["uri"]].append(e["position"])

    items_payload = [
        {"uri": uri, "positions": positions}
        for uri, positions in items_map.items()
    ]

    # This removes only the specific playlist positions you chose
    sp.playlist_remove_specific_occurrences_of_items(
        playlist_id, items_payload
    )
    print("Duplicate occurrences removed.\n")


def handle_duplicates(sp, playlist_id, tracks, keep_set):
    """
    Group tracks by (normalized title + main artist) and let the user
    remove extra copies (exact duplicates or different album versions).

    BEHAVIOR:
    - Enter numbers to KEEP (e.g. '2' or '1,3') -> remove all others in that group.
    - Enter '-2,3' to REMOVE those entries and keep the others.
    - Press Enter to keep all.
    - Type 'q' to stop duplicate review and apply removals so far.
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

        # duration
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

    print("\n=== Duplicate review ===")
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
                commit_duplicate_removals(sp, playlist_id, dup_removals)
                return

            if not resp:
                # keep all entries in this group
                # mark all as kept in cache
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
                    # treat as keep all
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
                            {"uri": e["uri"], "position": e["playlist_index"]}
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
                            {"uri": e["uri"], "position": e["playlist_index"]}
                        )

            break

    commit_duplicate_removals(sp, playlist_id, dup_removals)


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

    # Step 1: handle duplicates (same song, exact or remastered/original)
    handle_duplicates(sp, playlist_id, tracks, keep_set)

    # Step 2: re-fetch playlist and do year-based cleanup
    tracks = get_all_tracks(sp, playlist_id)
    review_tracks_by_year(sp, playlist_id, tracks, cutoff_year, keep_set)

    # Save updated keep_set back to cache
    decision_cache["keep"] = sorted(keep_set)
    save_decision_cache(decision_cache)


if __name__ == "__main__":
    main()
