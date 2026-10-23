import asyncio
import html
import sys
import threading
from pathlib import Path

import httpx
from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot
from PySide6.QtGui import QCloseEvent, QFont
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QDialog,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from services.local_profile import LocalProfile
from services.artwork import apply_playlist_artwork
from services.slskd_container import SlskdContainer
from services.soulseek import SearchUnavailableError, SoulseekService
from services.spotify import SpotifyService


class DownloadWorker(QObject):
    progress = Signal(int, int, str)
    log = Signal(str)
    succeeded = Signal(str)
    artwork_ready = Signal(str, object, object)
    failed = Signal(str)
    cancelled = Signal()
    finished = Signal()

    def __init__(
        self,
        playlist_url: str,
        username: str,
        password: str,
        download_directory: str,
    ) -> None:
        super().__init__()
        self.playlist_url = playlist_url
        self.username = username
        self.password = password
        self.download_directory = Path(download_directory).expanduser().resolve()
        self.cancel_requested = threading.Event()

    @Slot()
    def cancel(self) -> None:
        self.cancel_requested.set()

    @Slot()
    def run(self) -> None:
        try:
            self._run()
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()

    def _run(self) -> None:
        if not self.download_directory.is_absolute() or not self.download_directory.is_dir():
            raise ValueError("Choose an existing absolute download folder.")

        profile = LocalProfile()
        container = SlskdContainer(profile.directory)
        self.log.emit("Starting the local Soulseek service…")
        slskd_url, api_key = container.ensure_running(
            self.username, self.password, self.download_directory
        )
        profile.save(
            {
                "soulseek_username": self.username,
                "soulseek_password": self.password,
                "download_directory": str(self.download_directory),
            }
        )

        self.log.emit("Reading playlist metadata from Spotify…")
        playlist = SpotifyService().get_playlist_details(self.playlist_url)
        self.log.emit(f"Found {len(playlist.tracks)} tracks in “{playlist.name}”.")
        downloaded_files = asyncio.run(self._download_tracks(playlist, slskd_url, api_key))
        if self.cancel_requested.is_set():
            self.cancelled.emit()
            return
        image_url = SpotifyService.get_playlist_image_url(playlist)
        image = self._download_playlist_image(image_url) if image_url else None
        if image:
            self.artwork_ready.emit(playlist.name, image, downloaded_files)
        self.succeeded.emit(f"Download session finished for “{playlist.name}”.")

    @staticmethod
    def _download_playlist_image(image_url: str | None) -> bytes | None:
        if not image_url:
            return None
        import httpx

        response = httpx.get(image_url, timeout=30.0)
        response.raise_for_status()
        return response.content

    async def _download_tracks(self, playlist, slskd_url: str, api_key: str) -> list[Path]:
        service = SoulseekService(slskd_url, api_key)
        total = len(playlist.tracks)
        downloaded_files: list[Path] = []
        for index, track in enumerate(playlist.tracks, start=1):
            name = track.track.name
            artist = track.track.artists[0].name
            album = track.track.album.name if track.track.album else ""
            queries = [
                f"{artist} {name} {album}".strip(),
                f"{name} {album}".strip(),
                f"{artist} {name}".strip(),
                name,
            ]
            self.progress.emit(index - 1, total, name)
            self.log.emit(f"[INFO] [{index}/{total}] searching title, artist, and album")
            try:
                result = await service.download_track(
                    search_query=queries,
                    expected_duration_sec=round(track.track.duration_ms / 1000),
                    playlist_name=playlist.name,
                    download_directory=self.download_directory,
                    log=self.log.emit,
                    cancelled=self.cancel_requested.is_set,
                )
            except SearchUnavailableError as exc:
                self.log.emit(f"[ERROR] Search unavailable: {exc}")
                result = None
            if not result:
                self.log.emit(f"[NOT FOUND] {name}")
            else:
                downloaded_files.append(result)
            if self.cancel_requested.is_set():
                return downloaded_files
        self.progress.emit(total, total, "complete")
        return downloaded_files


class DiagnosticWorker(QObject):
    report = Signal(str)
    finished = Signal()

    def __init__(self, username: str, password: str, download_directory: str, repair: bool) -> None:
        super().__init__()
        self.username = username
        self.password = password
        self.download_directory = Path(download_directory).expanduser().resolve()
        self.repair = repair

    @Slot()
    def run(self) -> None:
        try:
            profile = LocalProfile()
            container = SlskdContainer(profile.directory)
            if self.repair:
                self.report.emit("[INFO] repairing slskd using the current form values…")
                if not self.download_directory.is_dir():
                    raise ValueError("choose an existing absolute download folder first")
                base_url, api_key = container.ensure_running(
                    self.username, self.password, self.download_directory
                )
                self.report.emit("[OK] slskd was started and soulseek login is ready")
            else:
                status = container.status()
                self.report.emit(
                    f"[INFO] docker container: {status.get('status', 'unknown')}"
                )
                if status.get("status") != "running":
                    self.report.emit("[ERROR] slskd is not running; click repair slskd")
                    return
                credentials = container.api_credentials()
                if credentials is None:
                    self.report.emit("[ERROR] slskd api credentials are missing")
                    return
                base_url, api_key = credentials

            headers = {"X-API-Key": api_key}
            with httpx.Client(base_url=base_url, headers=headers, timeout=8.0) as client:
                response = client.get("/api/v0/application")
                response.raise_for_status()
                application = response.json()
                server = application.get("server", {})
                if server.get("isConnected") and server.get("isLoggedIn"):
                    self.report.emit("[OK] soulseek is connected and logged in")
                else:
                    state = server.get("state", "unknown")
                    self.report.emit(
                        f"[ERROR] soulseek is not ready (state: {state}); "
                        "check the username and password, then click repair slskd"
                    )
                transfers = client.get("/api/v0/transfers/downloads")
                transfers.raise_for_status()
                self.report.emit("[OK] slskd download api is responding")
                search = client.post("/api/v0/searches", json={"searchText": "test"})
                search.raise_for_status()
                search_id = search.json().get("id")
                if search_id:
                    client.delete(f"/api/v0/searches/{search_id}")
                self.report.emit("[OK] slskd search api is accepting searches")
                self.report.emit(
                    "[INFO] if searches still return no songs, try a broader query "
                    "or allow more time for soulseek peers to respond"
                )
        except Exception as exc:
            self.report.emit(f"[ERROR] diagnostic failed: {exc}")
        finally:
            self.finished.emit()


class SlskdDiagnoserDialog(QDialog):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("slskd diagnoser")
        self.setMinimumSize(560, 300)
        layout = QVBoxLayout(self)
        title = QLabel("slskd diagnoser")
        title.setObjectName("section")
        layout.addWidget(title)
        layout.addWidget(QLabel(
            "checks docker, the slskd api, soulseek login, and download readiness"
        ))
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        layout.addWidget(self.view, stretch=1)
        buttons = QHBoxLayout()
        self.diagnose_button = QPushButton("run diagnosis")
        self.repair_button = QPushButton("repair slskd")
        self.close_button = QPushButton("close")
        for button in (self.diagnose_button, self.repair_button):
            button.setObjectName("secondary")
        self.close_button.clicked.connect(self.close)
        buttons.addWidget(self.diagnose_button)
        buttons.addWidget(self.repair_button)
        buttons.addStretch()
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

    def closeEvent(self, event: QCloseEvent) -> None:
        parent = self.parent()
        if isinstance(parent, MainWindow) and parent.diagnostic_thread:
            QMessageBox.information(
                self,
                "diagnosis in progress",
                "wait for the diagnosis to finish before closing.",
            )
            event.ignore()
            return
        event.accept()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.thread: QThread | None = None
        self.worker: DownloadWorker | None = None
        self.diagnostic_thread: QThread | None = None
        self.diagnostic_worker: DiagnosticWorker | None = None
        self.diagnoser_dialog: SlskdDiagnoserDialog | None = None
        self.setWindowTitle("spotlist downloader")
        self.setMinimumSize(760, 650)
        self.resize(920, 760)
        self._build_ui()

    def _build_ui(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget { background: white; color: #202733; }
            QFrame#card { background: white; border: 1px solid #e1e6ed; border-radius: 12px; }
            QLabel#brand { color: #18202b; font-size: 25px; font-weight: 700; }
            QLabel#subtitle, QLabel#hint, QLabel#privacy { color: #748094; }
            QLabel#section { color: #526074; font-size: 12px; font-weight: 700; }
            QLineEdit { background: white; border: 1px solid #dce2ea; border-radius: 6px; padding: 10px; }
            QLineEdit:focus { border: 1px solid #94b847; }
            QPushButton { border: 0; border-radius: 6px; padding: 10px 16px; }
            QPushButton#primary { background: #c8f36b; color: #18202b; font-weight: 700; }
            QPushButton#primary:hover { background: #b8e15e; }
            QPushButton#secondary { background: #e9edf2; color: #425064; }
            QPushButton:disabled { color: #9aa3b0; background: #edf0f3; }
            QPlainTextEdit { background: #18202b; color: #c8f36b; border-radius: 8px; padding: 10px; }
            QProgressBar { border: 0; border-radius: 4px; background: #e8edf1; text-align: center; height: 9px; }
            QProgressBar::chunk { border-radius: 4px; background: #a9d34c; }
            """
        )
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(42, 34, 42, 34)
        outer.setSpacing(20)

        header = QHBoxLayout()
        title_box = QVBoxLayout()
        brand = QLabel("spotlist downloader")
        brand.setObjectName("brand")
        subtitle = QLabel("a local workspace for bringing your playlists home.")
        subtitle.setObjectName("subtitle")
        title_box.addWidget(brand)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()
        self.status = QLabel("●  ready")
        self.status.setStyleSheet("color: #749536; font-weight: 600;")
        header.addWidget(self.status, alignment=Qt.AlignmentFlag.AlignTop)
        outer.addLayout(header)

        card = QFrame()
        card.setObjectName("card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(28, 25, 28, 28)
        card_layout.setSpacing(15)
        section = QLabel("new download")
        section.setObjectName("section")
        card_layout.addWidget(section)
        hint = QLabel("spotify metadata and soulseek credentials remain on this device.")
        hint.setObjectName("hint")
        card_layout.addWidget(hint)

        form = QFormLayout()
        form.setHorizontalSpacing(20)
        form.setVerticalSpacing(12)
        self.playlist = QLineEdit()
        self.playlist.setPlaceholderText("https://open.spotify.com/playlist/…")
        self.username = QLineEdit()
        self.username.setPlaceholderText("soulseek username")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("soulseek password")
        self.directory = QLineEdit()
        self.directory.setPlaceholderText("/home/you/Music")
        browse = QPushButton("browse…")
        browse.setObjectName("secondary")
        browse.clicked.connect(self._choose_directory)
        directory_box = QHBoxLayout()
        directory_box.addWidget(self.directory)
        directory_box.setSpacing(10)
        directory_box.addWidget(browse)
        form.addRow("spotify playlist url", self.playlist)
        form.addRow("soulseek username", self.username)
        form.addRow("soulseek password", self.password)
        form.addRow("download folder", directory_box)
        card_layout.addLayout(form)

        actions = QHBoxLayout()
        self.start_button = QPushButton("start download  →")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(self._start_download)
        self.cancel_button = QPushButton("cancel download")
        self.cancel_button.setObjectName("secondary")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel_download)
        actions.addStretch()
        actions.addWidget(self.start_button)
        actions.addWidget(self.cancel_button)
        card_layout.addLayout(actions)
        diagnoser_button = QPushButton("slskd diagnoser")
        diagnoser_button.setObjectName("secondary")
        diagnoser_button.clicked.connect(self._open_diagnoser)
        card_layout.addWidget(diagnoser_button, alignment=Qt.AlignmentFlag.AlignRight)
        outer.addWidget(card)

        progress_box = QVBoxLayout()
        self.progress_label = QLabel("ready for a playlist.")
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        progress_box.addWidget(self.progress_label)
        progress_box.addWidget(self.progress)
        outer.addLayout(progress_box)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setPlaceholderText("download activity will appear here.")
        self.log_view.setFont(QFont("Monospace", 10))
        outer.addWidget(self.log_view, stretch=1)
        privacy = QLabel("the revolution will not be streamed.")
        privacy.setObjectName("privacy")
        outer.addWidget(privacy, alignment=Qt.AlignmentFlag.AlignCenter)
        self.setCentralWidget(root)

    def _choose_directory(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "choose download folder")
        if directory:
            self.directory.setText(directory)

    def _start_download(self) -> None:
        values = [
            self.playlist.text().strip(),
            self.username.text().strip(),
            self.password.text(),
            self.directory.text().strip(),
        ]
        if not all(values):
            QMessageBox.warning(self, "missing details", "complete all fields before starting.")
            return
        self.start_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.status.setText("●  working…")
        self.status.setStyleSheet("color: #b17b32; font-weight: 600;")
        self.log_view.clear()
        self.thread = QThread(self)
        self.worker = DownloadWorker(*values)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self._append_log)
        self.worker.progress.connect(self._update_progress)
        self.worker.succeeded.connect(self._download_succeeded)
        self.worker.artwork_ready.connect(self._offer_playlist_artwork)
        self.worker.failed.connect(self._download_failed)
        self.worker.cancelled.connect(self._download_cancelled)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self._thread_finished)
        self.thread.start()

    def _cancel_download(self) -> None:
        if self.worker:
            self.cancel_button.setEnabled(False)
            self.status.setText("●  cancelling…")
            self._append_log("[INFO] cancellation requested; waiting for the current request to finish")
            self.worker.cancel()

    @Slot(int, int, str)
    def _update_progress(self, current: int, total: int, name: str) -> None:
        self.progress.setRange(0, max(total, 1))
        self.progress.setValue(current)
        self.progress_label.setText(
            f"downloading: {name.lower()}" if current < total else "download session complete."
        )
        if current < total:
            self.status.setText(f"●  working — downloading {current + 1} of {total}")

    @Slot(str)
    def _append_log(self, message: str) -> None:
        message = message.lower()
        escaped = html.escape(message)
        if message.startswith(("[error]", "[waited too long]")):
            color = "#d64545"
        elif message.startswith("[not found]"):
            color = "#e59b2a"
        elif message.startswith("[downloaded]"):
            color = "#54b86a"
        elif message.startswith("[cancelled]"):
            color = "#8b95a3"
        elif message.startswith(("[info]", "[waiting]")):
            color = "#4c9be8"
        elif message.startswith("[found]"):
            color = "#9b7ed0"
        else:
            color = "#4c9be8"
        self.log_view.appendHtml(f'<span style="color:{color}">{escaped}</span>')

    @Slot(str)
    def _download_succeeded(self, message: str) -> None:
        self.status.setText("●  ready — download session complete")
        self.status.setStyleSheet("color: #749536; font-weight: 600;")
        self._append_log(f"[INFO] {message}")

    @Slot(str)
    def _download_failed(self, message: str) -> None:
        self.status.setText("●  error — download stopped")
        self.status.setStyleSheet("color: #b84e3d; font-weight: 600;")
        self._append_log(f"[ERROR] {message}")

    @Slot()
    def _download_cancelled(self) -> None:
        self.status.setText("●  ready — download cancelled")
        self.status.setStyleSheet("color: #8b95a3; font-weight: 600;")
        self._append_log("[CANCELLED] download cancelled by the user")

    @Slot(str, object, object)
    def _offer_playlist_artwork(self, playlist_name: str, image: object, files: object) -> None:
        paths = [path for path in files if isinstance(path, Path)]
        if not paths or not isinstance(image, bytes):
            return
        answer = QMessageBox.question(
            self,
            "use spotify playlist artwork?",
            f"apply the spotify artwork for “{playlist_name.lower()}” to the metadata of "
            f"{len(paths)} downloaded song(s)?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            self._append_log("[INFO] Kept the artwork already embedded in downloaded songs.")
            return
        updated, failures = apply_playlist_artwork(paths, image)
        self._append_log(f"[INFO] Updated playlist artwork in {updated} song(s).")
        for failure in failures:
            self._append_log(f"[ERROR] Could not update artwork: {failure}")

    def _run_diagnosis(self) -> None:
        self._start_diagnostic(False)

    def _repair_slskd(self) -> None:
        self._start_diagnostic(True)

    def _start_diagnostic(self, repair: bool) -> None:
        if self.diagnoser_dialog is None:
            return
        if self.diagnostic_thread and self.diagnostic_thread.isRunning():
            return
        self.diagnoser_dialog.view.clear()
        self.diagnoser_dialog.diagnose_button.setEnabled(False)
        self.diagnoser_dialog.repair_button.setEnabled(False)
        self.diagnostic_thread = QThread(self)
        self.diagnostic_worker = DiagnosticWorker(
            self.username.text().strip(),
            self.password.text(),
            self.directory.text().strip(),
            repair,
        )
        self.diagnostic_worker.moveToThread(self.diagnostic_thread)
        self.diagnostic_thread.started.connect(self.diagnostic_worker.run)
        self.diagnostic_worker.report.connect(self._append_diagnostic)
        self.diagnostic_worker.finished.connect(self.diagnostic_thread.quit)
        self.diagnostic_worker.finished.connect(self.diagnostic_worker.deleteLater)
        self.diagnostic_thread.finished.connect(self._diagnostic_finished)
        self.diagnostic_thread.start()

    @Slot(str)
    def _append_diagnostic(self, message: str) -> None:
        if self.diagnoser_dialog:
            self.diagnoser_dialog.view.appendPlainText(message.lower())

    def _diagnostic_finished(self) -> None:
        if self.diagnoser_dialog:
            self.diagnoser_dialog.diagnose_button.setEnabled(True)
            self.diagnoser_dialog.repair_button.setEnabled(True)
        self.diagnostic_thread = None
        self.diagnostic_worker = None

    def _open_diagnoser(self) -> None:
        if self.diagnoser_dialog is None:
            self.diagnoser_dialog = SlskdDiagnoserDialog(self)
            self.diagnoser_dialog.diagnose_button.clicked.connect(self._run_diagnosis)
            self.diagnoser_dialog.repair_button.clicked.connect(self._repair_slskd)
            self.diagnoser_dialog.finished.connect(self._diagnoser_closed)
        self.diagnoser_dialog.show()
        self.diagnoser_dialog.raise_()
        self.diagnoser_dialog.activateWindow()

    def _diagnoser_closed(self) -> None:
        self.diagnoser_dialog = None

    def _thread_finished(self) -> None:
        self.start_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.thread = None
        self.worker = None

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.thread and self.thread.isRunning():
            QMessageBox.information(
                self,
                "download in progress",
                "wait for the current download to finish before closing.",
            )
            event.ignore()
            return
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("spotlist downloader")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
