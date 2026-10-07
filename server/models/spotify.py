from typing import List, Optional
from pydantic import BaseModel

class TrackModel(BaseModel):
    track_id: int
    title: str
    artist: str
    primary_artist:str
    album: str
    duration_ms: int
    duration_sec : int

class PlaylistResponse(BaseModel):
    playlist_id: str
    playlist_name: str
    cover_image_url: Optional[str] = None
    total_tracks: int
    tracks: List[TrackModel]