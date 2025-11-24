# Spotify Playlist Cleaner

Small helper script to tidy a Spotify playlist.

- Finds **duplicate tracks** (original vs remaster, same song twice) and lets you choose which copies to remove.
- Lets you pick a **cutoff year** (default 1992) and review tracks from albums released **after** that year, one by one.
- Nothing is removed without your confirmation.

## Requirements

- Python 3.8+
- Install Spotipy:
  - `pip install spotipy`
- Spotify Developer app:
  1. Go to https://developer.spotify.com/dashboard and **Create app** (type: Web API).
  2. In app settings, add redirect URI: `http://127.0.0.1:8888/callback`.
  3. Copy the app **Client ID** and **Client Secret**.

Set these environment variables before running:

- `SPOTIPY_CLIENT_ID` = your Client ID
- `SPOTIPY_CLIENT_SECRET` = your Client Secret
- `SPOTIPY_REDIRECT_URI` = `http://127.0.0.1:8888/callback`

## Usage

1. Run the script:

   - `python spotify_playlist_cleaner.py`

2. First run: a browser opens → log in to Spotify and approve the app.
3. Choose a playlist by number.
4. Choose a cutoff year (press Enter for 1992).
5. Review:
   - Duplicates: choose which entries (if any) to delete.
   - Year filter: for each track with album year > cutoff, choose keep/delete.
6. Confirm deletions when asked; if you answer **No**, nothing in Spotify is changed.
