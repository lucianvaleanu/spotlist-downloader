from fastapi import APIRouter, HTTPException, Query
from spotify_scraper import Playlist

from services.spotify import SpotifyService

router = APIRouter(prefix="/api", tags=["spotify"])
spotify_service = SpotifyService()

@router.get("/playlist", response_model=Playlist)
async def get_playlist(url: str = Query(..., description="Spotify Playlist URL or ID")):
        data = spotify_service.get_playlist_details(url)
        if not data:
            raise HTTPException(status_code=404, detail="Playlist not found")

        return data