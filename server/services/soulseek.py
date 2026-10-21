import asyncio
import os
import re
import shutil
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()


class SearchUnavailableError(RuntimeError):
    pass


class SoulseekService:
    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        self.base_url = base_url or os.getenv("SLSKD_HOST", "http://localhost:5030")
        api_key = api_key or os.getenv("SLSKD_API_KEY")
        self.headers = {"Content-Type": "application/json"}
        if api_key:
            self.headers["X-API-Key"] = api_key

    async def download_track(
        self,
        search_query: str,
        expected_duration_sec: int,
        playlist_name: str,
        download_directory: Path,
        duration_tolerance_sec: int = 4,
        download_timeout_sec: int = 300,
    ) -> bool:
        async with httpx.AsyncClient(
            base_url=self.base_url, headers=self.headers, timeout=30.0
        ) as client:
            candidates = await self._find_candidates(
                client, search_query, expected_duration_sec, duration_tolerance_sec
            )
            if not candidates:
                print(f"[NOT FOUND] {search_query}")
                return False

            for extension in (".flac", ".mp3"):
                matching = [
                    candidate
                    for candidate in candidates
                    if candidate["filename"].lower().endswith(extension)
                ]
                if not matching:
                    if extension == ".mp3":
                        print(f"[.MP3 NOT FOUND] {search_query}")
                    else:
                        print(f"[.FLAC NOT FOUND] {search_query}; falling back to MP3")
                    continue

                for candidate in matching:
                    result = await self._queue_and_wait(
                        client,
                        candidate,
                        playlist_name,
                        download_directory,
                        download_timeout_sec,
                    )
                    if result:
                        return True
                    print(
                        f"[WAITED TOO LONG] {search_query}; trying another source"
                    )

        return False

    async def _find_candidates(
        self,
        client: httpx.AsyncClient,
        search_query: str,
        expected_duration_sec: int,
        duration_tolerance_sec: int,
    ) -> list[dict[str, Any]] | None:
        try:
            response = await client.post(
                "/api/v0/searches", json={"searchText": search_query}
            )
            response.raise_for_status()
            search_id = response.json()["id"]
            await asyncio.sleep(5)
            response = await client.get(f"/api/v0/searches/{search_id}/responses")
            response.raise_for_status()
            candidates = self._pick_candidates(
                response.json(), expected_duration_sec, duration_tolerance_sec
            )
            await client.delete(f"/api/v0/searches/{search_id}")
            return candidates or None
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise SearchUnavailableError(
                f"{type(exc).__name__}: {exc or 'no response from slskd'}"
            ) from exc

    async def _queue_and_wait(
        self,
        client: httpx.AsyncClient,
        candidate: dict[str, Any],
        playlist_name: str,
        download_directory: Path,
        timeout_sec: int,
    ) -> bool:
        username = candidate["username"]
        filename = candidate["filename"]
        extension = Path(filename.replace("\\", "/")).suffix.lower()
        print(f"[FOUND] {filename}")

        try:
            response = await client.post(
                f"/api/v0/transfers/downloads/{username}",
                json=[{"filename": filename, "size": candidate["size"]}],
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            print(f"[DOWNLOAD ERROR] {filename}: {exc}")
            return False

        print(f"[DOWNLOADING] {filename}")
        deadline = asyncio.get_running_loop().time() + timeout_sec
        while asyncio.get_running_loop().time() < deadline:
            try:
                transfer = await self._find_transfer(client, username, filename)
            except httpx.HTTPError as exc:
                print(f"[STATUS ERROR] {filename}: {exc}")
                await asyncio.sleep(5)
                continue
            if transfer and transfer["state"].startswith("Completed, Succeeded"):
                source = self._find_downloaded_file(download_directory, filename)
                if source is None:
                    print(f"[DOWNLOAD ERROR] completed file was not found: {filename}")
                    return False
                destination = self._destination_path(
                    download_directory, playlist_name, filename, extension
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), destination)
                print(f"[DOWNLOADED] {destination}")
                return True
            if transfer and (
                "Failed" in transfer["state"]
                or "Errored" in transfer["state"]
                or "Cancelled" in transfer["state"]
            ):
                return False
            print(f"[AWAITING DOWNLOAD] {filename}")
            await asyncio.sleep(5)

        return False

    async def _find_transfer(
        self, client: httpx.AsyncClient, username: str, filename: str
    ) -> dict[str, Any] | None:
        response = await client.get("/api/v0/transfers/downloads")
        response.raise_for_status()
        for peer in response.json():
            if peer.get("username") != username:
                continue
            for directory in peer.get("directories", []):
                for transfer in directory.get("files", []):
                    if transfer.get("filename") == filename:
                        return transfer
        return None

    def _pick_candidates(
        self,
        responses: list[dict[str, Any]],
        expected_duration: int,
        tolerance: int,
    ) -> list[dict[str, Any]]:
        candidates = []
        for peer in responses:
            for file_info in peer.get("files", []):
                filename = file_info.get("filename", "")
                if not filename.lower().endswith((".flac", ".mp3")):
                    continue
                file_length = file_info.get("length", 0)
                if file_length > 10000:
                    file_length = round(file_length / 1000)
                if expected_duration > 0 and file_length > 0:
                    if abs(file_length - expected_duration) > tolerance:
                        continue
                queue_length = peer.get("queueLength", 999)
                candidates.append(
                    {
                        "username": peer.get("username"),
                        "filename": filename,
                        "size": file_info.get("size", 0),
                        "score": (
                            (100 if peer.get("hasFreeUploadSlot", False) else 0)
                            + (1000 / (queue_length + 1))
                            + (peer.get("uploadSpeed", 0) / 10000)
                        ),
                    }
                )
        candidates.sort(key=lambda candidate: candidate["score"], reverse=True)
        return candidates

    @staticmethod
    def _find_downloaded_file(download_directory: Path, remote_filename: str) -> Path | None:
        basename = Path(remote_filename.replace("\\", "/")).name
        matches = list(download_directory.rglob(basename))
        return next((match for match in matches if match.is_file()), None)

    @staticmethod
    def _destination_path(
        download_directory: Path,
        playlist_name: str,
        remote_filename: str,
        extension: str,
    ) -> Path:
        safe_playlist = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", playlist_name).strip()
        safe_song = re.sub(
            r'[<>:"/\\|?*\x00-\x1f]',
            "_",
            Path(remote_filename.replace("\\", "/")).stem,
        ).strip()
        return download_directory / safe_playlist / f"{safe_song}{extension}"
