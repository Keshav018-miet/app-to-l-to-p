import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from android_utils import AndroidAppManager, ApkExtractor, AppData
from stream_engine import P2PStreamReceiver, TransferProgress


def progress_callback(progress: TransferProgress):
    if progress.error:
        print(f"| ERROR: {progress.error}")
    else:
        status = "COMPLETED" if progress.is_completed else "STREAMING"
        print(
            f"[{status}] File: {progress.current_file_path} | "
            f"Progress: {progress.percentage:.1f}% | "
            f"Speed: {progress.speed_mbs:.2f} MB/s | "
            f"ETA: {progress.eta_seconds:.1f}s"
        )


async def main():
    # 1. Initialize AndroidAppManager
    print("--- Querying Application List ---")
    app_manager = AndroidAppManager()
    apps = app_manager.get_installed_apps()
    
    print(f"Detected {len(apps)} applications:")
    for app in apps:
        app_type = "System App" if app.is_system else "User App"
        print(f" - {app.name} [{app.package_id}] ({app_type}) | Path: {app.apk_path}")

    if not apps:
        print("[-] No applications detected. Exiting.")
        return

    # 2. Setup temporary receiver destination
    temp_dir = Path(tempfile.gettempdir())
    dest_dir = temp_dir / "p2p_apk_destination"
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir()
    print(f"\nReceiver Destination folder: {dest_dir}")

    # 3. Start local loopback receiver server on port 8888
    port = 8888
    # The streamer on receiver side uses the progress callback
    receiver = P2PStreamReceiver(port=port, dest_dir=dest_dir, progress_callback=progress_callback)
    server_task = asyncio.create_task(receiver.start())
    await asyncio.sleep(0.5)  # Wait for server to bind

    # 4. Extract and stream the first mock application (WhatsApp)
    selected_app = apps[0]
    print(f"\n--- Extracting & Streaming APK: {selected_app.name} ({selected_app.package_id}) ---")
    
    extractor = ApkExtractor(progress_callback=progress_callback)
    
    try:
        await extractor.stream_apk(selected_app, "127.0.0.1", port)
        print("[+] APK stream finished on sender side.")
    except Exception as e:
        print(f"[-] Extraction stream failed: {e}")

    await asyncio.sleep(0.5)  # Wait for file output to complete on disk

    # Close server
    await receiver.stop()
    try:
        await server_task
    except asyncio.CancelledError:
        pass

    # 5. Verify the APK exists in receiver destination
    print("\n--- Verifying Reconstructed APK ---")
    expected_apk = dest_dir / f"{selected_app.package_id}.apk"
    if expected_apk.exists():
        size_mb = expected_apk.stat().st_size / (1024 * 1024)
        print(f"[SUCCESS] APK reconstructed successfully at: {expected_apk}")
        print(f"[SUCCESS] Reconstructed APK Size: {size_mb:.2f} MB")
        
        # Quick validation of contents (since we filled it with 'A's on Windows mock)
        sample = expected_apk.read_bytes()[:10]
        print(f"[SUCCESS] Sample bytes verification: {sample}")
    else:
        print(f"[FAILURE] Expected APK not found at: {expected_apk}")

    # Clean up destination
    print("\nCleaning up temporary files...")
    shutil.rmtree(dest_dir)
    print("Clean up finished.")


if __name__ == "__main__":
    asyncio.run(main())
