from fastapi import APIRouter, HTTPException, Query
from spotify_scraper import Playlist

from services.spotify import SpotifyService

router = APIRouter(prefix="/api", tags=["spotify"])
spotify_service = SpotifyService()

@router.get("/playlist", response_model=Playlist)
async def get_playlist(url: str = Query(..., description="Spotify Playlist URL or ID")):
    try:
        data = spotify_service.get_playlist_details(url)
        return data
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch playlist: {str(e)}")
