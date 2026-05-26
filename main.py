import asyncio
import os
import sys
import socket
import threading
from pathlib import Path
from typing import Dict, List, Optional

# Kivy / KivyMD main imports
from kivy.config import Config
# Prevent full screen lock on Windows for debugging
Config.set('graphics', 'resizable', '1')

from kivy.app import App
from kivy.clock import Clock
from kivy.lang import Builder
from kivy.uix.screenmanager import Screen
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label

from kivymd.app import MDApp
from kivymd.uix.tab import MDTabsBase
from kivymd.uix.card import MDCard
from kivymd.uix.list import OneLineAvatarIconListItem, IconLeftWidget, IRightBodyTouch
from kivymd.uix.selectioncontrol import MDCheckbox
from kivymd.uix.dialog import MDDialog
from kivymd.uix.button import MDRaisedButton, MDFlatButton
from kivymd.uix.progressbar import MDProgressBar

# mDNS discovery using zeroconf
from zeroconf import Zeroconf, ServiceBrowser, ServiceListener

# Import local engines and models
from models.device_info import DeviceInfo
from android_utils import AndroidAppManager, AppData, ApkExtractor
from stream_engine import ChunkedStreamer, TransferProgress, P2PStreamSender


# Native Pyjnius checks for Android specific functions (Vibrator)
IS_ANDROID = False
try:
    from jnius import autoclass
    autoclass('org.kivy.android.PythonActivity')
    IS_ANDROID = True
except Exception:
    pass


class CheckboxRightWidget(IRightBodyTouch, MDCheckbox):
    """Custom checkbox that fits inside ListItem lists."""
    pass


class AppListItem(OneLineAvatarIconListItem):
    """Custom Kivy list item representable in MDList."""
    def __init__(self, app_data: AppData, on_toggle, **kwargs):
        super().__init__(**kwargs)
        self.app_data = app_data
        self.text = app_data.name
        
        # Left Icon
        icon_name = "laptop-windows" if app_data.is_system else "android"
        self.add_widget(IconLeftWidget(icon=icon_name))
        
        # Right Checkbox
        self.checkbox = CheckboxRightWidget()
        self.checkbox.bind(active=lambda checkbox, value: on_toggle(app_data, value))
        self.add_widget(self.checkbox)


class FileListItem(OneLineAvatarIconListItem):
    """Custom File tree representation list item."""
    def __init__(self, file_path: Path, on_toggle, **kwargs):
        super().__init__(**kwargs)
        self.file_path = file_path
        self.text = file_path.name
        
        # Left Icon
        icon = "folder" if file_path.is_dir() else "file-document"
        self.add_widget(IconLeftWidget(icon=icon))
        
        # Right Checkbox
        self.checkbox = CheckboxRightWidget()
        self.checkbox.bind(active=lambda checkbox, value: on_toggle(file_path, value))
        self.add_widget(self.checkbox)


# Custom listener class to handle zeroconf mDNS scanner callbacks
class DeviceDiscoveryListener(ServiceListener):
    def __init__(self, add_callback, remove_callback):
        self.add_callback = add_callback
        self.remove_callback = remove_callback

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name)
        if info:
            ip = socket.inet_ntoa(info.addresses[0]) if info.addresses else "127.0.0.1"
            port = info.port
            
            # Parse TXT properties
            props = {}
            for k, v in info.properties.items():
                key_str = k.decode('utf-8') if isinstance(k, bytes) else str(k)
                val_str = v.decode('utf-8') if isinstance(v, bytes) else str(v)
                props[key_str] = val_str
                
            device_id = props.get('id', name.split('.')[0])
            friendly_name = props.get('name', name)
            os_name = props.get('os', 'Unknown')

            device = DeviceInfo(
                id=device_id,
                name=friendly_name,
                os=os_name,
                ipAddress=ip,
                port=port
            )
            # Schedule UI update thread-safely
            Clock.schedule_once(lambda dt: self.add_callback(device))

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        Clock.schedule_once(lambda dt: self.remove_callback(name))

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        pass


# KV layout string implementing Bottom Navigation Tabs
KV = """
BoxLayout:
    orientation: 'vertical'
    
    MDTopAppBar:
        title: "LocalDrop Mobile Share"
        elevation: 4
        md_bg_color: app.theme_cls.primary_color
        right_action_items: [["refresh", lambda x: app.refresh_all()]]
        
    MDBottomNavigation:
        panel_color: app.theme_cls.bg_normal
        
        MDBottomNavigationItem:
            name: 'screen_devices'
            text: 'Devices'
            icon: 'laptop'
            
            BoxLayout:
                orientation: 'vertical'
                padding: 16
                spacing: 12
                
                MDLabel:
                    text: "DISCOVERED LAPTOPS ON WI-FI"
                    font_style: "Button"
                    theme_text_color: "Hint"
                    size_hint_y: None
                    height: 24
                    
                ScrollView:
                    MDList:
                        id: device_list
                        
        MDBottomNavigationItem:
            name: 'screen_apps'
            text: 'Apps'
            icon: 'android'
            
            BoxLayout:
                orientation: 'vertical'
                padding: 16
                
                ScrollView:
                    MDList:
                        id: app_list
                        
        MDBottomNavigationItem:
            name: 'screen_photos'
            text: 'Photos'
            icon: 'image-multiple'
            
            BoxLayout:
                orientation: 'vertical'
                padding: 16
                
                ScrollView:
                    MDList:
                        id: photo_list
                        
        MDBottomNavigationItem:
            name: 'screen_files'
            text: 'Files'
            icon: 'folder-open'
            
            BoxLayout:
                orientation: 'vertical'
                padding: 16
                
                ScrollView:
                    MDList:
                        id: file_list
"""


class MobileDropApp(MDApp):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.selected_files: List[Path] = []
        self.selected_apps: List[AppData] = []
        self.discovered_devices: Dict[str, DeviceInfo] = {}
        
        self.zeroconf: Optional[Zeroconf] = None
        self.browser: Optional[ServiceBrowser] = None
        self.progress_dialog: Optional[MDDialog] = None
        self.app_manager = AndroidAppManager()

    def build(self):
        self.theme_cls.theme_style = "Dark"
        self.theme_cls.primary_palette = "LightBlue"
        self.theme_cls.accent_palette = "Teal"
        return Builder.load_string(KV)

    def on_start(self):
        self.refresh_all()
        self._start_mdns_discovery()

    def _trigger_haptic(self):
        """Vibrates for 40ms to provide premium tactile response."""
        if IS_ANDROID:
            try:
                PythonActivity = autoclass('org.kivy.android.PythonActivity')
                activity = PythonActivity.mActivity
                Context = autoclass('android.content.Context')
                vibrator = activity.getSystemService(Context.VIBRATOR_SERVICE)
                vibrator.vibrate(40)
            except Exception as e:
                print(f"[Haptic Error] {e}")
        else:
            print("[Haptic Feedback] (Vibration simulated)")

    def refresh_all(self):
        """Re-scans files, photos, apps and refreshes UI fields."""
        self._trigger_haptic()
        self._load_apps()
        self._load_photos()
        self._load_files()

    def _load_apps(self):
        """Populates Apps tab from PackageManager wrapper."""
        app_list_widget = self.root.ids.app_list
        app_list_widget.clear_widgets()
        
        apps = self.app_manager.get_installed_apps()
        for app in apps:
            item = AppListItem(app, self._on_app_toggled)
            app_list_widget.add_widget(item)

    def _load_photos(self):
        """Scans DCIM camera directories for user photo lists."""
        photo_list_widget = self.root.ids.photo_list
        photo_list_widget.clear_widgets()
        
        # Resolve target photos dir based on platform
        if IS_ANDROID:
            # Android external storage DCIM
            photo_dir = Path("/sdcard/DCIM/Camera")
            if not photo_dir.exists():
                photo_dir = Path("/sdcard/DCIM")
        else:
            # Windows simulated user pictures folder
            photo_dir = Path(os.path.expanduser('~')) / "Pictures"

        # Check fallback
        if not photo_dir.exists():
            photo_dir = Path(tempfile.gettempdir()) / "mock_photos"
            photo_dir.mkdir(exist_ok=True)
            # Create some mock images for simulation
            for i in range(1, 4):
                (photo_dir / f"DSC_000{i}.jpg").write_text(f"mock image data {i}")

        try:
            # Fetch common image extensions
            extensions = ('*.jpg', '*.jpeg', '*.png', '*.gif')
            photo_files = []
            for ext in extensions:
                photo_files.extend(photo_dir.glob(ext))
                
            for path in sorted(photo_files, reverse=True)[:25]: # limit to latest 25
                item = FileListItem(path, self._on_file_toggled)
                photo_list_widget.add_widget(item)
        except Exception as e:
            print(f"Error loading photos: {e}")

    def _load_files(self):
        """Displays simple list of user directories."""
        file_list_widget = self.root.ids.file_list
        file_list_widget.clear_widgets()
        
        if IS_ANDROID:
            base_dir = Path("/sdcard/Documents")
            if not base_dir.exists():
                base_dir = Path("/sdcard")
        else:
            base_dir = Path(os.path.expanduser('~')) / "Documents"

        if not base_dir.exists():
            base_dir = Path(os.getcwd())

        try:
            # Load top 15 files and subfolders
            count = 0
            for path in base_dir.iterdir():
                if count >= 20:
                    break
                # Only show directory entries or documents
                if path.name.startswith('.'):
                    continue
                item = FileListItem(path, self._on_file_toggled)
                file_list_widget.add_widget(item)
                count += 1
        except Exception as e:
            print(f"Error loading files: {e}")

    def _on_app_toggled(self, app_data: AppData, is_selected: bool):
        self._trigger_haptic()
        if is_selected:
            self.selected_apps.append(app_data)
        else:
            self.selected_apps.remove(app_data)
        print(f"Apps selected: {[a.package_id for a in self.selected_apps]}")

    def _on_file_toggled(self, file_path: Path, is_selected: bool):
        self._trigger_haptic()
        if is_selected:
            self.selected_files.append(file_path)
        else:
            self.selected_files.remove(file_path)
        print(f"Files selected: {[f.name for f in self.selected_files]}")

    def _start_mdns_discovery(self):
        """Starts scanning for remote _flutterp2p._tcp desktop endpoints."""
        self.zeroconf = Zeroconf()
        listener = DeviceDiscoveryListener(self._add_discovered_device, self._remove_discovered_device)
        self.browser = ServiceBrowser(self.zeroconf, "_flutterp2p._tcp.local.", listener)

    def _add_discovered_device(self, device: DeviceInfo):
        if device.id in self.discovered_devices:
            return
            
        self.discovered_devices[device.id] = device
        
        # Create an MDCard widget dynamically for this device
        device_card = MDCard(
            orientation='vertical',
            padding=16,
            size_hint_y=None,
            height=100,
            ripple_behavior=True,
            on_release=lambda x: self._on_device_card_clicked(device)
        )
        
        layout = BoxLayout(orientation='horizontal', spacing=12)
        layout.add_widget(Label(
            text=f"[b]{device.name}[/b]\nIP: {device.ipAddress}:{device.port} ({device.os})",
            markup=True,
            color=(1, 1, 1, 1),
            font_size='14sp',
            halign='left',
            valign='middle'
        ))
        
        device_card.add_widget(layout)
        # Store widget reference to remove it later if offline
        device.widget_ref = device_card
        
        self.root.ids.device_list.add_widget(device_card)
        print(f"[Discovery] Added discovered device to UI: {device.name}")

    def _remove_discovered_device(self, service_name: str):
        # Scan discovered devices for corresponding service name
        target_id = None
        for dev_id, dev in self.discovered_devices.items():
            if service_name.startswith(f"p2p-{dev_id}") or dev_id in service_name:
                target_id = dev_id
                break
                
        if target_id and target_id in self.discovered_devices:
            device = self.discovered_devices.pop(target_id)
            if hasattr(device, 'widget_ref'):
                self.root.ids.device_list.remove_widget(device.widget_ref)
            print(f"[Discovery] Removed device from UI: {device.name}")

    def _on_device_card_clicked(self, device: DeviceInfo):
        """Initializes direct async file/folder/APK share pipeline."""
        if not self.selected_files and not self.selected_apps:
            # Show standard warning dialog
            self.show_dialog("No Selection", "Please select at least one Photo, File, or App to share first.")
            return

        self._trigger_haptic()
        self._start_transfer_flow(device)

    def show_dialog(self, title: str, text: str):
        dialog = MDDialog(
            title=title,
            text=text,
            buttons=[MDRaisedButton(text="OK", on_release=lambda x: dialog.dismiss())]
        )
        dialog.open()

    def _start_transfer_flow(self, device: DeviceInfo):
        """Prepares dialog UI and executes asynchronous transfer thread."""
        # 1. Create dialog with progress bar
        self.progress_bar = MDProgressBar(value=0, max=100)
        self.dialog_details = Label(text="Initializing transmission...", font_size='12sp', color=(1, 1, 1, 1))
        
        content = BoxLayout(orientation='vertical', spacing=8, size_hint_y=None, height=60)
        content.add_widget(self.progress_bar)
        content.add_widget(self.dialog_details)

        self.progress_dialog = MDDialog(
            title=f"Transferring to {device.name}",
            type="custom",
            content_cls=content,
            auto_dismiss=False,
            buttons=[MDFlatButton(text="CANCEL", on_release=self._cancel_transfer)]
        )
        self.progress_dialog.open()

        # 2. Spawn worker thread
        self.worker_thread = threading.Thread(target=self._run_transfer_pipeline, args=(device,))
        self.worker_thread.daemon = True
        self.worker_thread.start()

    def _cancel_transfer(self, dialog):
        self.progress_dialog.dismiss()
        self.show_dialog("Transfer Cancelled", "The transfer has been interrupted.")

    def _run_transfer_pipeline(self, device: DeviceInfo):
        """Synchronizes pipeline operations and maps updates to the Main thread."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # Thread-safe progress update bridge
        def progress_callback(progress: TransferProgress):
            def update_ui_elements():
                self.progress_bar.value = int(progress.percentage)
                if progress.error:
                    self.dialog_details.text = f"Error: {progress.error}"
                else:
                    self.dialog_details.text = (
                        f"File: {progress.current_file_path}\n"
                        f"Progress: {progress.percentage:.1f}% | {progress.speed_mbs:.2f} MB/s"
                    )
            # Invoke on main Kivy clock thread
            Clock.schedule_once(lambda dt: update_ui_elements())

        try:
            # A) Process selected APK files
            if self.selected_apps:
                extractor = ApkExtractor(progress_callback=progress_callback)
                for app in self.selected_apps:
                    loop.run_until_complete(extractor.stream_apk(app, device.ipAddress, device.port))

            # B) Process selected general files/folders
            if self.selected_files:
                sender = P2PStreamSender(
                    host=device.ipAddress, 
                    port=device.port, 
                    progress_callback=progress_callback
                )
                for path in self.selected_files:
                    if path.is_file():
                        # Single-file wrap: we can just copy to a temp directory and stream,
                        # or create a quick temporary manifest
                        # Let's use a temp dir copy to stream it with 100% engine compatibility
                        with tempfile.TemporaryDirectory() as temp_dir:
                            temp_path = Path(temp_dir) / path.name
                            shutil.copy2(path, temp_path)
                            loop.run_until_complete(sender.send_directory(Path(temp_dir)))
                    elif path.is_dir():
                        loop.run_until_complete(sender.send_directory(path))

            # Success trigger
            Clock.schedule_once(lambda dt: self._on_pipeline_success())

        except Exception as e:
            print(f"[Pipeline Error] {e}")
            Clock.schedule_once(lambda dt: self._on_pipeline_error(str(e)))
        finally:
            loop.close()

    def _on_pipeline_success(self):
        self._trigger_haptic()
        if self.progress_dialog:
            self.progress_dialog.dismiss()
        self.show_dialog("Transfer Complete", "All selected items have been successfully shared!")
        self.selected_files.clear()
        self.selected_apps.clear()
        self.refresh_all()

    def _on_pipeline_error(self, err_msg: str):
        if self.progress_dialog:
            self.progress_dialog.dismiss()
        self.show_dialog("Transfer Failed", f"An error occurred during transmission:\n{err_msg}")

    def on_stop(self):
        # Stop background scanning browser
        if self.browser:
            self.browser.cancel()
        if self.zeroconf:
            self.zeroconf.close()


import tempfile
import shutil
if __name__ == '__main__':
    MobileDropApp().run()
