import sys
import os
import asyncio
import platform
import subprocess
from pathlib import Path
from PyQt6.QtCore import QThread, pyqtSignal, Qt, QSize
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QProgressBar, QPushButton, QSystemTrayIcon, QMenu, QFrame
)
from PyQt6.QtGui import QAction, QIcon, QFont, QColor

# Import streaming classes from stream_engine
from stream_engine import P2PStreamReceiver, TransferProgress


class AsyncReceiverWorker(QThread):
    """QThread hosting the asyncio Event Loop to run P2PStreamReceiver."""
    progress_signal = pyqtSignal(object)  # Emits TransferProgress
    complete_signal = pyqtSignal(str)     # Emits absolute file path of completed file
    log_signal = pyqtSignal(str)          # Emits log strings

    def __init__(self, port: int, dest_dir: Path):
        super().__init__()
        self.port = port
        self.dest_dir = dest_dir
        self.loop = None
        self.receiver = None
        self.server_task = None

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        # Signal bridge callback
        def progress_callback(progress: TransferProgress):
            self.progress_signal.emit(progress)
            if progress.is_completed and not progress.error:
                # Resolve destination path
                final_path = str(self.dest_dir / progress.current_file_path)
                self.complete_signal.emit(final_path)

        self.receiver = P2PStreamReceiver(
            port=self.port,
            dest_dir=self.dest_dir,
            progress_callback=progress_callback
        )

        self.log_signal.emit(f"Server binding to port {self.port}...")
        self.server_task = self.loop.create_task(self.receiver.start())

        try:
            self.loop.run_forever()
        except Exception as e:
            self.log_signal.emit(f"Event loop exception: {e}")
        finally:
            if self.server_task:
                self.server_task.cancel()
                # Run loop to allow cleanup of cancellation
                self.loop.run_until_complete(asyncio.gather(self.server_task, return_exceptions=True))
            self.loop.close()
            self.log_signal.emit("Server background thread stopped.")

    def stop(self):
        """Safely stops receiver and loop from another thread."""
        if self.receiver and self.loop:
            # Shutdown the asyncio server
            asyncio.run_coroutine_threadsafe(self.receiver.stop(), self.loop)
            # Terminate loop.run_forever()
            self.loop.call_soon_threadsafe(self.loop.stop)


class ModernDropWindow(QMainWindow):
    """Sleek, Windows 11 style UI for receiving local P2P drops."""

    def __init__(self):
        super().__init__()
        self.port = 9090
        # Default save directory: Downloads/LocalDrop
        self.dest_dir = Path(os.path.expanduser('~')) / 'Downloads' / 'LocalDrop'
        self.dest_dir.mkdir(parents=True, exist_ok=True)

        self.last_completed_file = None
        self.is_transferring = False

        self._setup_ui()
        self._setup_tray()
        self._start_server_thread()

    def _setup_ui(self):
        self.setWindowTitle("LocalDrop Desktop Receiver")
        self.setMinimumSize(480, 420)
        self.resize(520, 450)

        # Style Application (Fluent Dark Theme)
        self.setStyleSheet("""
            QMainWindow {
                background-color: #0F172A; /* Slate 900 */
            }
            QLabel {
                color: #F8FAFC; /* Slate 50 */
                font-family: "Segoe UI", Arial;
            }
            QLabel#titleLabel {
                font-size: 20px;
                font-weight: bold;
                color: #38BDF8; /* Neon Light Blue */
            }
            QLabel#statusText {
                font-size: 13px;
                color: #94A3B8; /* Slate 400 */
            }
            QFrame#cardFrame {
                background-color: #1E293B; /* Slate 800 */
                border: 1px solid #334155; /* Slate 700 */
                border-radius: 12px;
            }
            QProgressBar {
                border: 1px solid #334155;
                border-radius: 6px;
                text-align: center;
                background-color: #0F172A;
                color: #F8FAFC;
                font-weight: bold;
                height: 24px;
            }
            QProgressBar::chunk {
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #38BDF8, stop:1 #34D399);
                border-radius: 5px;
            }
            QPushButton {
                background-color: #38BDF8;
                color: #0F172A;
                border: none;
                border-radius: 8px;
                padding: 10px 18px;
                font-size: 13px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #7DD3FC;
            }
            QPushButton:pressed {
                background-color: #0284C7;
            }
            QPushButton#secondaryBtn {
                background-color: #1E293B;
                color: #F8FAFC;
                border: 1px solid #334155;
                border-radius: 8px;
                padding: 10px 18px;
            }
            QPushButton#secondaryBtn:hover {
                background-color: #334155;
            }
            QPushButton#secondaryBtn:pressed {
                background-color: #475569;
            }
        """)

        # Main layout
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(24, 24, 24, 24)
        main_layout.setSpacing(16)

        # Header Title
        self.title_label = QLabel("LocalDrop Receiver")
        self.title_label.setObjectName("titleLabel")
        main_layout.addWidget(self.title_label)

        # Server Status Subtext
        self.status_label = QLabel(f"Server inactive. Destination: {self.dest_dir.name}")
        self.status_label.setObjectName("statusText")
        main_layout.addWidget(self.status_label)

        # Info/Progress Card Container
        self.card = QFrame()
        self.card.setObjectName("cardFrame")
        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(20, 20, 20, 20)
        card_layout.setSpacing(14)

        # Peer and File info labels
        self.transfer_title = QLabel("Ready for incoming files...")
        self.transfer_title.setStyleSheet("font-size: 16px; font-weight: bold;")
        card_layout.addWidget(self.transfer_title)

        self.file_label = QLabel("No active drop detected.")
        self.file_label.setStyleSheet("color: #94A3B8; font-size: 13px;")
        card_layout.addWidget(self.file_label)

        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.hide()
        card_layout.addWidget(self.progress_bar)

        # Stats horizontal pane (Speed, ETA)
        self.stats_layout = QHBoxLayout()
        self.speed_label = QLabel("")
        self.speed_label.setStyleSheet("font-size: 12px; color: #38BDF8; font-weight: bold;")
        self.eta_label = QLabel("")
        self.eta_label.setStyleSheet("font-size: 12px; color: #34D399; font-weight: bold;")
        self.stats_layout.addWidget(self.speed_label)
        self.stats_layout.addWidget(self.eta_label)
        card_layout.addLayout(self.stats_layout)

        main_layout.addWidget(self.card)

        # Action Buttons layout
        self.btn_layout = QHBoxLayout()
        self.btn_layout.setSpacing(12)

        self.btn_open = QPushButton("Open File")
        self.btn_open.clicked.connect(self._open_file)
        self.btn_open.hide()

        self.btn_explore = QPushButton("Show in Folder")
        self.btn_explore.setObjectName("secondaryBtn")
        self.btn_explore.clicked.connect(self._explore_file)
        self.btn_explore.hide()

        self.btn_layout.addWidget(self.btn_open)
        self.btn_layout.addWidget(self.btn_explore)
        main_layout.addLayout(self.btn_layout)

        # Spacer at bottom
        main_layout.addStretch()

    def _setup_tray(self):
        """Creates system tray utility to run server in background."""
        self.tray_icon = QSystemTrayIcon(self)
        
        # Use standard system style icon for simplicity (no assets needed)
        icon = self.style().standardIcon(QApplication.style().StandardPixmap.SP_ComputerIcon)
        self.tray_icon.setIcon(icon)
        self.tray_icon.setToolTip("LocalDrop Receiver Active")

        # Tray actions
        show_action = QAction("Open LocalDrop", self)
        show_action.triggered.connect(self.show_normal)
        
        exit_action = QAction("Quit Application", self)
        exit_action.triggered.connect(self._exit_app)

        tray_menu = QMenu()
        tray_menu.addAction(show_action)
        tray_menu.addSeparator()
        tray_menu.addAction(exit_action)
        
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.show()

        # Handle double click on tray icon
        self.tray_icon.activated.connect(self._on_tray_activated)

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_normal()

    def _start_server_thread(self):
        """Spins up the QThread receiver engine."""
        self.worker = AsyncReceiverWorker(self.port, self.dest_dir)
        self.worker.progress_signal.connect(self._on_transfer_progress)
        self.worker.complete_signal.connect(self._on_transfer_complete)
        self.worker.log_signal.connect(self._on_server_log)
        self.worker.start()

    def _on_server_log(self, log_msg: str):
        print(f"[Worker Log] {log_msg}")
        if "binding" in log_msg.lower() or "port" in log_msg.lower():
            self.status_label.setText(f"Active. Listening on port {self.port} | Path: Downloads/LocalDrop")

    def _on_transfer_progress(self, progress: TransferProgress):
        """Triggered automatically cross-thread when chunks stream in."""
        if progress.error:
            self.is_transferring = False
            self.transfer_title.setText("Transfer Interrupted")
            self.file_label.setText(f"Error: {progress.error}")
            self.progress_bar.setStyleSheet("QProgressBar::chunk { background-color: #EF4444; }")
            return

        # Restore window if hidden/minimized on first chunks (Auto Pop-up)
        if not self.is_transferring and progress.percentage > 0:
            self.is_transferring = True
            self.progress_bar.setStyleSheet("") # Restore gradient styling
            self.progress_bar.show()
            self.btn_open.hide()
            self.btn_explore.hide()
            self.show_normal()

        # Update UI labels
        self.transfer_title.setText("Receiving Drop...")
        self.file_label.setText(f"File: {progress.current_file_path}")
        self.progress_bar.setValue(int(progress.percentage))
        
        speed_mb = progress.speed_mbs
        self.speed_label.setText(f"Speed: {speed_mb:.2f} MB/s")
        self.eta_label.setText(f"ETA: {progress.eta_seconds:.1f}s")

        if progress.is_completed:
            self.is_transferring = False

    def _on_transfer_complete(self, final_file_path: str):
        """Triggered upon successful transmission and write of base file."""
        self.last_completed_file = final_file_path
        self.transfer_title.setText("Drop Completed Successfully!")
        self.file_label.setText(f"Saved: {Path(final_file_path).name}")
        self.progress_bar.setValue(100)
        self.speed_label.setText("")
        self.eta_label.setText("")

        # Show explorer quick action buttons
        self.btn_open.show()
        self.btn_explore.show()

        # Display system balloon notification
        self.tray_icon.showMessage(
            "LocalDrop Complete",
            f"Successfully received {Path(final_file_path).name}!",
            QSystemTrayIcon.MessageIcon.Information,
            3000
        )

    def _open_file(self):
        """Launches file with default Windows handler."""
        if self.last_completed_file and os.path.exists(self.last_completed_file):
            try:
                os.startfile(self.last_completed_file)
            except Exception as e:
                print(f"Failed to open file: {e}")

    def _explore_file(self):
        """Highlights file inside explorer folder."""
        if self.last_completed_file and os.path.exists(self.last_completed_file):
            try:
                # Format explorer parameters with absolute double-quoted paths
                subprocess.run(['explorer', '/select,', os.path.abspath(self.last_completed_file)])
            except Exception as e:
                print(f"Failed to locate file: {e}")

    def show_normal(self):
        """Restores window focus and raises it above other applications."""
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.activateWindow()
        self.raise_()

    def changeEvent(self, event):
        """Override minimize action to push to system tray."""
        if event.type() == event.Type.WindowStateChange:
            if self.isMinimized():
                # Hide window
                self.hide()
                # Notify tray
                self.tray_icon.showMessage(
                    "LocalDrop minimized",
                    "Receiver is still listening for phone drops in the background.",
                    QSystemTrayIcon.MessageIcon.Information,
                    2000
                )
                event.accept()
                return
        super().changeEvent(event)

    def closeEvent(self, event):
        """Instead of closing server, close button also minimizes to tray."""
        self.hide()
        event.ignore()

    def _exit_app(self):
        """Real application exit trigger."""
        print("[App] Shutting down LocalDrop Receiver...")
        self.worker.stop()
        self.worker.wait() # Wait for background QThread loop to shutdown
        self.tray_icon.hide()
        QApplication.quit()


def main():
    app = QApplication(sys.argv)
    
    window = ModernDropWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
