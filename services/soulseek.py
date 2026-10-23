import asyncio
import os
import re
import shutil
from pathlib import Path
from typing import Any, Callable

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
        search_query: str | list[str],
        expected_duration_sec: int,
        playlist_name: str,
        download_directory: Path,
        duration_tolerance_sec: int = 4,
        download_timeout_sec: int = 300,
        log: Callable[[str], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> Path | None:
        emit = log or print
        queries = [search_query] if isinstance(search_query, str) else search_query
        async with httpx.AsyncClient(
            base_url=self.base_url, headers=self.headers, timeout=30.0
        ) as client:
            for query in dict.fromkeys(query for query in queries if query):
                if cancelled and cancelled():
                    emit("[CANCELLED] download cancelled by the user")
                    return None
                emit(f"[INFO] trying search strategy: {query}")
                candidates = await self._find_candidates(
                    client, query, expected_duration_sec, duration_tolerance_sec
                )
                if not candidates:
                    emit(f"[NOT FOUND] no results for: {query}")
                    continue

                for extension in (".flac", ".mp3"):
                    matching = [
                        candidate
                        for candidate in candidates
                        if candidate["filename"].lower().endswith(extension)
                    ]
                    if not matching:
                        if extension == ".mp3":
                            emit(f"[NOT FOUND] mp3 fallback unavailable for: {query}")
                        else:
                            emit(f"[INFO] flac not found; falling back to mp3 for: {query}")
                        continue

                    for candidate in matching:
                        result = await self._queue_and_wait(
                            client,
                            candidate,
                            playlist_name,
                            download_directory,
                            download_timeout_sec,
                            emit,
                            cancelled,
                        )
                        if result:
                            return result
                        if cancelled and cancelled():
                            emit("[CANCELLED] download cancelled by the user")
                            return None
                        emit(f"[WAITED TOO LONG] trying another source for: {query}")

        emit(f"[NOT FOUND] no downloadable flac or mp3 source for: {queries[0] if queries else ''}")
        return None

    async def _find_candidates(
        self,
        client: httpx.AsyncClient,
        search_query: str,
        expected_duration_sec: int,
        duration_tolerance_sec: int,
    ) -> list[dict[str, Any]] | None:
        search_id: str | None = None
        try:
            for attempt in range(3):
                try:
                    response = await client.post(
                        "/api/v0/searches", json={"searchText": search_query}
                    )
                    response.raise_for_status()
                    search_id = response.json()["id"]
                    break
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code != 409 or attempt == 2:
                        detail = exc.response.text.strip()
                        raise SearchUnavailableError(
                            "slskd rejected the search with HTTP "
                            f"{exc.response.status_code}"
                            + (f": {detail}" if detail else "")
                        ) from exc
                    await asyncio.sleep(10)

            if search_id is None:
                raise SearchUnavailableError("slskd did not return a search id")

            await asyncio.sleep(5)
            response = await client.get(f"/api/v0/searches/{search_id}/responses")
            response.raise_for_status()
            candidates = self._pick_candidates(
                response.json(), expected_duration_sec, duration_tolerance_sec
            )
            return candidates or None
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise SearchUnavailableError(
                f"{type(exc).__name__}: {exc or 'no response from slskd'}"
            ) from exc
        finally:
            if search_id is not None:
                try:
                    await client.delete(f"/api/v0/searches/{search_id}")
                except httpx.HTTPError:
                    pass

    async def _queue_and_wait(
        self,
        client: httpx.AsyncClient,
        candidate: dict[str, Any],
        playlist_name: str,
        download_directory: Path,
        timeout_sec: int,
        log: Callable[[str], None],
        cancelled: Callable[[], bool] | None,
    ) -> Path | None:
        username = candidate["username"]
        filename = candidate["filename"]
        extension = Path(filename.replace("\\", "/")).suffix.lower()
        log(f"[FOUND] candidate: {filename}")

        try:
            response = await client.post(
                f"/api/v0/transfers/downloads/{username}",
                json=[{"filename": filename, "size": candidate["size"]}],
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log(f"[ERROR] Download request failed for {filename}: {exc}")
            return None

        log(f"[INFO] Downloading {filename}")
        deadline = asyncio.get_running_loop().time() + timeout_sec
        while asyncio.get_running_loop().time() < deadline:
            if cancelled and cancelled():
                log(f"[CANCELLED] stopped waiting for {filename}")
                return None
            try:
                transfer = await self._find_transfer(client, username, filename)
            except httpx.HTTPError as exc:
                log(f"[ERROR] Status check failed for {filename}: {exc}")
                await asyncio.sleep(5)
                continue
            if transfer and transfer["state"].startswith("Completed, Succeeded"):
                source = self._find_downloaded_file(download_directory, filename)
                if source is None:
                    log(f"[ERROR] Completed file was not found: {filename}")
                    return None
                destination = self._destination_path(
                    download_directory, playlist_name, filename, extension
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), destination)
                log(f"[DOWNLOADED] {destination.name}")
                return destination
            if transfer and (
                "Failed" in transfer["state"]
                or "Errored" in transfer["state"]
                or "Cancelled" in transfer["state"]
            ):
                log(f"[ERROR] Soulseek could not download {filename}")
                return None
            log(f"[WAITING] waiting for {filename}")
            await asyncio.sleep(5)

        log(f"[ERROR] Timed out downloading {filename}")
        return None

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
