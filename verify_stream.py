import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from stream_engine import FolderSerializer, P2PStreamSender, P2PStreamReceiver, TransferProgress


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
    # 1. Setup Temporary Directories for Verification
    temp_dir = Path(tempfile.gettempdir())
    src_dir = temp_dir / "p2p_test_source"
    dest_dir = temp_dir / "p2p_test_destination"

    # Clean previous test directories if any
    if src_dir.exists():
        shutil.rmtree(src_dir)
    if dest_dir.exists():
        shutil.rmtree(dest_dir)

    src_dir.mkdir()
    dest_dir.mkdir()

    print(f"Source Dir: {src_dir}")
    print(f"Dest Dir:   {dest_dir}")

    # Create dummy nested directories and files
    (src_dir / "folder1").mkdir()
    (src_dir / "folder2" / "subfolder").mkdir(parents=True)

    # 10MB large file to test 4MB chunking
    large_data = b"X" * (10 * 1024 * 1024)
    (src_dir / "large_file.bin").write_bytes(large_data)
    (src_dir / "folder1" / "info.txt").write_text("Hello from P2P Stream Engine!")
    (src_dir / "folder2" / "subfolder" / "config.json").write_text('{"port": 8080, "authorized": true}')

    print("\n--- Folder Structure Serialized ---")
    manifest = FolderSerializer.serialize(src_dir)
    print(f"Found {len(manifest['files'])} files in manifest.")
    for f in manifest['files']:
        print(f" - {f['rel_path']} ({f['size'] / (1024*1024):.2f} MB)")

    # 2. Setup Server and Client for P2P Local stream
    port = 9999
    receiver = P2PStreamReceiver(port=port, dest_dir=dest_dir, progress_callback=progress_callback)
    sender = P2PStreamSender(host="127.0.0.1", port=port, progress_callback=progress_callback)

    # Start receiver server in the background
    server_task = asyncio.create_task(receiver.start())
    
    # Allow server to bind and start listening
    await asyncio.sleep(0.5)

    print("\n--- Commencing P2P Transfer ---")
    # Trigger Sender connection
    try:
        await sender.send_directory(src_dir)
        print("Transfer completed on sender side.")
    except Exception as e:
        print(f"Transfer Error: {e}")

    # Allow receiver to write out final buffers and close handles
    await asyncio.sleep(0.5)

    # Stop the receiver server
    await receiver.stop()
    try:
        await server_task
    except asyncio.CancelledError:
        pass

    # 3. Verify files and folders are identical
    print("\n--- Verifying Reconstructed Directory ---")
    success = True
    for f in manifest['files']:
        rel_path = f['rel_path']
        src_file = src_dir / rel_path
        dest_file = dest_dir / rel_path

        if not dest_file.exists():
            print(f"[-] Missing file in destination: {rel_path}")
            success = False
            continue

        src_size = src_file.stat().st_size
        dest_size = dest_file.stat().st_size

        if src_size != dest_size:
            print(f"[-] Size mismatch for {rel_path}: Src={src_size}, Dest={dest_size}")
            success = False
        else:
            print(f"[+] Verified {rel_path} matches source size ({src_size} bytes)")

    if success:
        print("\n[SUCCESS] All files and folders successfully serialized, streamed, and reconstructed!")
    else:
        print("\n[FAILURE] Discrepancies found in reconstructed directory.")

    # 4. Clean up test directories
    print("Cleaning up temporary test files...")
    shutil.rmtree(src_dir)
    shutil.rmtree(dest_dir)
    print("Clean up finished.")


if __name__ == "__main__":
    asyncio.run(main())
