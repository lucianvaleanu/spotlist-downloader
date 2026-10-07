import os
import re
from typing import Dict, Any, List
import spotipy
from spotipy.oauth2 import  SpotifyOAuth
from dotenv import load_dotenv

load_dotenv()

class SpotifyService:
    def __init__(self):
        client_id = os.getenv("SPOTIFY_CLIENT_ID")
        client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
        if not client_id or not client_secret:
            raise ValueError("SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET must be set.")

        auth_manager = SpotifyOAuth(client_id=client_id, client_secret=client_secret,redirect_uri="http://127.0.0.1:9090")
        self.__sp = spotipy.Spotify(auth_manager=auth_manager)

    @staticmethod
    def extract_playlist_id(input_str: str) -> str:
        match = re.search(r"playlist/([a-zA-Z0-9]{22})", input_str)
        if match:
            return match.group(1)
        if re.fullmatch(r"[a-zA-Z0-9]{22}", input_str.strip()):
            return input_str.strip()
        raise ValueError("Invalid Spotify playlist URL or ID provided.")

    def get_playlist_details(self, playlist_url_or_id: str) -> Dict[str, Any]:
        playlist_id = self.extract_playlist_id(playlist_url_or_id)

        # fetch playlist info
        playlist = self.__sp.playlist(
            playlist_id,
            fields="name,images,tracks.total"
        )

        images = playlist.get("images") or []
        cover_url = images[0]["url"] if images else None

        # paginate through tracks
        tracks: List[Dict[str, Any]] = []
        offset = 0
        limit = 100

        while True:
            batch = self.__sp.playlist_items(
                playlist_id,
                fields="items.track(name,artists(name),duration_ms,album(name)),next",
                limit=limit,
                offset=offset
            )
            items = batch.get("items", [])
            if not items:
                break

            for item in items:
                track = item.get("track")
                if not track or not track.get("name"):
                    continue

                artists = [a["name"] for a in track.get("artists", [])]
                tracks.append({
                    "title": track.get("name"),
                    "artist": ", ".join(artists),
                    "primary_artist": artists[0] if artists else "Unknown",
                    "album": track.get("album", {}).get("name", ""),
                    "duration_ms": track.get("duration_ms", 0),
                    "duration_sec": round(track.get("duration_ms", 0) / 1000)
                })

            if not batch.get("next"):
                break
            offset += limit

        return {
            "playlist_id": playlist_id,
            "playlist_name": playlist.get("name"),
            "cover_image_url": cover_url,
            "total_tracks": len(tracks),
            "tracks": tracks
        }