from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from services.local_profile import LocalProfile
from services.slskd_container import SlskdContainer
from services.spotify import SpotifyService
from services.soulseek import SearchUnavailableError, SoulseekService

router = APIRouter(prefix="/api", tags=["download"])
spotify_service = SpotifyService()
profile = LocalProfile()
container_service = SlskdContainer(profile.directory)


class DownloadRequest(BaseModel):
    playlist_url: str = Field(min_length=1)
    soulseek_username: str = Field(min_length=1, max_length=64)
    soulseek_password: str = Field(min_length=1, max_length=256)
    download_directory: str = Field(min_length=1)


class SlskdStartRequest(BaseModel):
    soulseek_username: str = Field(min_length=1, max_length=64)
    soulseek_password: str = Field(min_length=1, max_length=256)
    download_directory: str = Field(min_length=1)


def start_slskd(payload: SlskdStartRequest) -> tuple[str, str, Path]:
    download_directory = Path(payload.download_directory).expanduser()
    if not download_directory.is_absolute() or not download_directory.is_dir():
        raise HTTPException(
            status_code=400,
            detail="download_directory must be an existing absolute directory",
        )

    try:
        slskd_url, api_key = container_service.ensure_running(
            payload.soulseek_username,
            payload.soulseek_password,
            download_directory,
        )
        profile.save(
            {
                "soulseek_username": payload.soulseek_username,
                "soulseek_password": payload.soulseek_password,
                "download_directory": str(download_directory),
            }
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    return slskd_url, api_key, download_directory


async def process_playlist_download(
    playlist_url: str,
    slskd_url: str,
    api_key: str,
    download_directory: Path,
):
    # 1. extract tracks from spotify
    playlist_data = spotify_service.get_playlist_details(playlist_url)
    tracks = playlist_data.tracks

    print(f"Starting download pipeline for: {playlist_data.name} ({len(tracks)} tracks)")
    soulseek_service = SoulseekService(slskd_url, api_key)

    # 2. iterate and trigger downloads sequentially (throttle concurrency)
    for track in tracks:
        query = f"{track.track.artists[0].name} {track.track.name}"
        # strip common noise: (Remastered...), (feat...)

        try:
            result = await soulseek_service.download_track(
                search_query=query,
                expected_duration_sec=round(track.track.duration_ms / 1000),
                playlist_name=playlist_data.name,
                download_directory=download_directory,
            )
        except SearchUnavailableError as exc:
            print(f"[SEARCH ERROR] {query}: {exc}")
            result = None

        if result is False:
            print(f"[NOT FOUND] {track.track.name}")


@router.post("/slskd/start")
async def start_slskd_container(payload: SlskdStartRequest):
    slskd_url, _, download_directory = start_slskd(payload)
    return {
        "status": "started",
        "message": "slskd is running.",
        "slskd_url": slskd_url,
        "download_directory": str(download_directory),
    }


@router.get("/slskd/status")
async def get_slskd_status():
    try:
        return container_service.status()
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.post("/slskd/stop")
async def stop_slskd_container():
    try:
        return container_service.stop()
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.post("/download")
async def trigger_download(payload: DownloadRequest, background_tasks: BackgroundTasks):
    try:
        spotify_service.get_playlist_details(payload.playlist_url)
        slskd_url, api_key, download_directory = start_slskd(
            SlskdStartRequest(
                soulseek_username=payload.soulseek_username,
                soulseek_password=payload.soulseek_password,
                download_directory=payload.download_directory,
            )
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    background_tasks.add_task(
        process_playlist_download,
        payload.playlist_url,
        slskd_url,
        api_key,
        download_directory,
    )
    return {
        "status": "accepted",
        "message": "slskd started and playlist download queued.",
        "download_directory": str(download_directory),
    }