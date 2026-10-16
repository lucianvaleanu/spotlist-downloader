from typing import List, Optional

class TrackModel:
    def __init__(self, track_id, title, artist, primary_artist, album, duration_ms, duration_sec):
        self.__track_id: int = track_id
        self.__title: str = title
        self.__artist: str = artist
        self.__primary_artist: str = primary_artist
        self.__album: str = album
        self.__duration_ms: int = duration_ms
        self.__duration_sec: int =  duration_sec

    def get_track_id(self) -> int:
        return self.__track_id

    def get_title(self) -> str:
        return self.__title

    def get_artist(self) -> str:
        return self.__artist

    def get_primary_artist(self) -> str:
        return self.__primary_artist

    def get_album(self) -> str:
        return self.__album

    def get_duration_ms(self) -> int:
        return self.__duration_ms

    def get_duration_sec(self) -> int:
        return self.__duration_sec


class PlaylistModel:
    def __init__(self, id, name, image, total_tracks, tracks):
        self.__playlist_id: str = id
        self.__playlist_name: str = name
        self.__cover_image_url: Optional[str] = image
        self.__total_tracks: int = total_tracks
        self.__tracks: List[TrackModel] = tracks

    def get_playlist_id(self) -> str:
        return self.__playlist_id

    def get_playlist_name(self) -> str:
        return self.__playlist_name

    def get_cover_image_url(self) -> Optional[str]:
        return self.__cover_image_url

    def get_total_tracks(self) -> int:
        return self.__total_tracks

    def get_tracks(self) -> List[TrackModel]:
        return self.__tracks

