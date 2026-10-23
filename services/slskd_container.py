import json
import os
import re
import secrets
import time
from pathlib import Path

import docker
import httpx
from docker.errors import DockerException, NotFound


class SlskdContainer:
    name = "spotlist-slskd"
    image = os.getenv("SLSKD_IMAGE", "slskd/slskd:latest")

    def __init__(self, profile_directory: Path) -> None:
        self.profile_directory = profile_directory
        self.app_directory = profile_directory / "slskd"
        self.app_directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    def ensure_running(
        self, username: str, password: str, download_directory: Path
    ) -> tuple[str, str]:
        download_directory = download_directory.expanduser().resolve()
        if not download_directory.is_absolute() or not download_directory.is_dir():
            raise ValueError("choose an existing download folder with an absolute path")
        host_user = f"{os.getuid()}:{os.getgid()}"
        self._prepare_app_directory()
        try:
            client = docker.from_env()
            container = client.containers.get(self.name)
        except NotFound:
            container = None
        except DockerException as exc:
            raise RuntimeError("Docker is not running or is not accessible") from exc

        if container is not None:
            container.reload()
            existing = self._existing_connection(
                container, username, password, download_directory, host_user
            )
            if container.status == "running" and existing is not None:
                self._wait_for_api(*existing)
                self._wait_for_soulseek(*existing)
                return existing

        api_key = secrets.token_urlsafe(32)
        self._write_config(username, password, api_key, download_directory)

        if container is not None:
            container.remove(force=True)

        try:
            container = client.containers.run(
                self.image,
                name=self.name,
                detach=True,
                user=host_user,
                ports={"5030/tcp": ("127.0.0.1", None)},
                volumes={
                    str(self.app_directory): {"bind": "/app", "mode": "rw"},
                    str(download_directory): {"bind": "/downloads", "mode": "rw"},
                },
            )
        except DockerException as exc:
            raise RuntimeError(f"Unable to start slskd: {exc}") from exc

        port = self._host_port(
            container.attrs.get("NetworkSettings", {}).get("Ports", {}),
            "5030/tcp",
        )
        if port is None:
            raise RuntimeError("slskd did not expose its API port")
        base_url = f"http://127.0.0.1:{port}"
        self._wait_for_api(base_url, api_key)
        self._wait_for_soulseek(base_url, api_key)
        return base_url, api_key

    def _prepare_app_directory(self) -> None:
        uid = os.getuid()
        gid = os.getgid()
        for path in (self.app_directory, *self.app_directory.rglob("*")):
            try:
                os.chown(path, uid, gid)
            except OSError as exc:
                raise RuntimeError(
                    "slskd app-data contains files owned by another user. "
                    f"Run: sudo chown -R {uid}:{gid} {self.app_directory}"
                ) from exc

    def status(self) -> dict[str, object]:
        client = self._docker_client()
        try:
            container = client.containers.get(self.name)
        except NotFound:
            return {
                "exists": False,
                "name": self.name,
                "status": "not_found",
            }

        container.reload()
        ports = container.attrs.get("NetworkSettings", {}).get("Ports", {})
        host_port = self._host_port(ports, "5030/tcp")
        health = container.attrs.get("State", {}).get("Health", {}).get("Status")

        return {
            "exists": True,
            "name": self.name,
            "id": container.short_id,
            "status": container.status,
            "health": health,
            "api_url": f"http://127.0.0.1:{host_port}" if host_port else None,
            "image": container.image.tags[0] if container.image.tags else None,
        }

    def api_credentials(self) -> tuple[str, str] | None:
        config_path = self.app_directory / "slskd.yml"
        if not config_path.exists():
            return None
        config = config_path.read_text(encoding="utf-8")
        api_key = re.search(r'key:\s*"([^"]+)"', config)
        status = self.status()
        api_url = status.get("api_url")
        if api_key is None or not isinstance(api_url, str):
            return None
        return api_url, api_key.group(1)

    def stop(self) -> dict[str, object]:
        client = self._docker_client()
        try:
            container = client.containers.get(self.name)
        except NotFound:
            return {
                "exists": False,
                "name": self.name,
                "status": "not_found",
            }

        container.reload()
        if container.status == "running":
            container.stop(timeout=10)
            container.reload()

        return {
            "exists": True,
            "name": self.name,
            "id": container.short_id,
            "status": container.status,
        }

    @staticmethod
    def _host_port(ports: dict[str, object], container_port: str) -> int | None:
        bindings = ports.get(container_port)
        if not bindings:
            return None
        return int(bindings[0]["HostPort"])

    @staticmethod
    def _docker_client():
        try:
            return docker.from_env()
        except DockerException as exc:
            raise RuntimeError("Docker is not running or is not accessible") from exc

    def _existing_connection(
        self,
        container: object,
        username: str,
        password: str,
        download_directory: Path,
        expected_user: str,
    ) -> tuple[str, str] | None:
        configured_user = container.attrs.get("Config", {}).get("User", "")
        if configured_user != expected_user:
            return None

        port = self._host_port(
            container.attrs.get("NetworkSettings", {}).get("Ports", {}),
            "5030/tcp",
        )
        if port is None:
            return None

        config_path = self.app_directory / "slskd.yml"
        if not config_path.exists():
            return None
        config = config_path.read_text(encoding="utf-8")
        configured_username = re.search(r'username:\s*"([^"]+)"', config)
        configured_password = re.search(r'password:\s*"([^"]+)"', config)
        api_key = re.search(r'key:\s*"([^"]+)"', config)
        if configured_username is None or configured_password is None or api_key is None:
            return None
        if configured_username.group(1) != username:
            return None
        if configured_password.group(1) != password:
            return None

        mounts = container.attrs.get("Mounts", [])
        download_mount = next(
            (mount for mount in mounts if mount.get("Destination") == "/downloads"),
            None,
        )
        if download_mount is None:
            return None
        if Path(download_mount["Source"]).resolve() != download_directory.resolve():
            return None
        return f"http://127.0.0.1:{port}", api_key.group(1)

    @staticmethod
    def _wait_for_api(base_url: str, api_key: str) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                response = httpx.get(
                    f"{base_url}/api/v0/transfers/downloads",
                    headers={"X-API-Key": api_key},
                    timeout=2,
                )
                if response.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        raise RuntimeError("Timed out waiting for slskd API to become ready")

    @staticmethod
    def _wait_for_soulseek(base_url: str, api_key: str) -> None:
        deadline = time.monotonic() + 60
        last_state = "unknown"
        while time.monotonic() < deadline:
            try:
                response = httpx.get(
                    f"{base_url}/api/v0/application",
                    headers={"X-API-Key": api_key},
                    timeout=2,
                )
                response.raise_for_status()
                server = response.json().get("server", {})
                last_state = server.get("state", "unknown")
                if server.get("isConnected") and server.get("isLoggedIn"):
                    return
            except (httpx.HTTPError, ValueError, AttributeError):
                pass
            time.sleep(1)
        raise RuntimeError(
            "slskd is running but is not connected to Soulseek "
            f"(state: {last_state}). Check the Soulseek credentials and slskd logs."
        )

    def _write_config(
        self, username: str, password: str, api_key: str, download_directory: Path
    ) -> None:
        config = "\n".join(
            [
                "soulseek:",
                f"  username: {json.dumps(username)}",
                f"  password: {json.dumps(password)}",
                "directories:",
                "  downloads: /downloads",
                "web:",
                "  ip_address: 0.0.0.0",
                "  authentication:",
                "    api_keys:",
                "      spotlist:",
                f"        key: {json.dumps(api_key)}",
                "        role: administrator",
            ]
        )
        config_path = self.app_directory / "slskd.yml"
        config_path.write_text(config + "\n", encoding="utf-8")
        config_path.chmod(0o600)

    @staticmethod
    def _wait_for_port(container: object) -> int:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            container.reload()
            if container.status == "exited":
                raise RuntimeError("slskd stopped during startup")
            ports = container.attrs.get("NetworkSettings", {}).get("Ports", {})
            binding = ports.get("5030/tcp")
            if binding:
                return int(binding[0]["HostPort"])
            time.sleep(0.25)
        raise RuntimeError("Timed out waiting for slskd to expose its API")
