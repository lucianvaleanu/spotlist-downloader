import spotify_scraper
from dotenv import load_dotenv
from spotify_scraper import Playlist


class SpotifyService:
    def __init__(self):
        self.__spotify_client = spotify_scraper.SpotifyClient()

    def get_playlist_details(self, playlist_url_or_id: str):
       playlist = self.__spotify_client.get_playlist(playlist_url_or_id)
       if not playlist:
           raise ValueError("Playlist not found")
       return playlist

    def get_soulseek_search_query(self, playlist: Playlist):
        playlist_tracks_string = []
        for _ in playlist.tracks:
            track_string = _.track.artists[0].name + " " + _.track.name
            playlist_tracks_string.append(track_string)
        return playlist_tracks_string



