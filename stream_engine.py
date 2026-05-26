import asyncio
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Callable, Optional

# Async file I/O package. If it's not installed, we can fall back to standard blocking run-in-executor, 
# but we recommend installing it. Let's import it safely.
try:
    import aiofiles
    HAS_AIOFILES = True
except ImportError:
    HAS_AIOFILES = False


@dataclass
class FileMetadata:
    rel_path: str
    size: int


@dataclass
class TransferProgress:
    total_bytes: int
    transferred_bytes: int
    current_file_path: str
    speed_mbs: float
    percentage: float
    eta_seconds: float
    is_completed: bool = False
    error: Optional[str] = None


class StreamEngineError(Exception):
    """Base exception for all stream engine errors."""
    pass


class FolderSerializer:
    """Utility to map and serialize relative directory trees."""

    @staticmethod
    def serialize(dir_path: Path) -> Dict:
        if not dir_path.is_dir():
            raise StreamEngineError(f"Path is not a directory: {dir_path}")

        files_metadata: List[Dict] = []
        # Walk recursively using pathlib
        for path in dir_path.rglob('*'):
            if path.is_file():
                # Get path relative to the base directory
                rel_path = path.relative_to(dir_path).as_posix()
                size = path.stat().st_size
                files_metadata.append({
                    "rel_path": rel_path,
                    "size": size
                })

        return {
            "base_name": dir_path.name,
            "files": files_metadata
        }

    @staticmethod
    def reconstruct_structure(base_dir: Path, manifest: Dict) -> None:
        """Pre-creates directories on the destination filesystem."""
        base_dir.mkdir(parents=True, exist_ok=True)
        for file_info in manifest.get("files", []):
            file_path = base_dir / file_info["rel_path"]
            # Create directories along the path if they don't exist
            file_path.parent.mkdir(parents=True, exist_ok=True)


class ChunkedStreamer:
    """Handles async chunked binary reads and writes with speed tracking."""
    
    CHUNK_SIZE = 4 * 1024 * 1024  # 4MB Chunks

    def __init__(self, progress_callback: Optional[Callable[[TransferProgress], None]] = None):
        self.progress_callback = progress_callback
        self._start_time = 0.0
        self._transferred_bytes = 0
        self._total_bytes = 0

    def _update_progress(self, current_file: str, bytes_chunk: int, total_bytes: int, error: Optional[str] = None) -> None:
        self._transferred_bytes += bytes_chunk
        self._total_bytes = total_bytes
        
        now = time.time()
        elapsed = now - self._start_time
        if elapsed <= 0:
            elapsed = 0.001

        speed = (self._transferred_bytes / elapsed) / (1024 * 1024)  # MB/s
        percent = (self._transferred_bytes / total_bytes * 100) if total_bytes > 0 else 100.0
        
        if speed > 0:
            eta = (total_bytes - self._transferred_bytes) / (speed * 1024 * 1024)
        else:
            eta = 9999.0

        progress = TransferProgress(
            total_bytes=total_bytes,
            transferred_bytes=min(self._transferred_bytes, total_bytes),
            current_file_path=current_file,
            speed_mbs=speed,
            percentage=min(percent, 100.0),
            eta_seconds=max(0.0, eta) if percent < 100.0 else 0.0,
            is_completed=(self._transferred_bytes >= total_bytes),
            error=error
        )

        if self.progress_callback:
            try:
                self.progress_callback(progress)
            except Exception:
                pass  # Avoid callback exceptions crashing the stream

    async def _read_file_chunk(self, file_path: Path, offset: int, size: int) -> bytes:
        """Reads a chunk from a file asynchronously."""
        if HAS_AIOFILES:
            async with aiofiles.open(file_path, mode='rb') as f:
                await f.seek(offset)
                return await f.read(size)
        else:
            # Fallback if aiofiles is missing
            def _read_sync():
                with open(file_path, 'rb') as f:
                    f.seek(offset)
                    return f.read(size)
            return await asyncio.get_event_loop().run_in_executor(None, _read_sync)

    async def _write_file_chunk(self, file_path: Path, offset: int, data: bytes) -> None:
        """Writes a chunk of data to a file asynchronously."""
        if HAS_AIOFILES:
            # Open with r+b if exists to write at offset, otherwise wb
            mode = 'r+b' if file_path.exists() else 'wb'
            async with aiofiles.open(file_path, mode=mode) as f:
                await f.seek(offset)
                await f.write(data)
        else:
            # Fallback if aiofiles is missing
            def _write_sync():
                mode = 'r+b' if file_path.exists() else 'wb'
                with open(file_path, mode=mode) as f:
                    f.seek(offset)
                    f.write(data)
            await asyncio.get_event_loop().run_in_executor(None, _write_sync)

    async def stream_folder_out(
        self, 
        source_dir: Path, 
        writer: asyncio.StreamWriter
    ) -> None:
        """Serializes and streams a folder structure out over TCP."""
        self._start_time = time.time()
        self._transferred_bytes = 0
        
        try:
            # 1. Generate and Send Manifest
            manifest = FolderSerializer.serialize(source_dir)
            manifest_bytes = json.dumps(manifest).encode('utf-8')
            
            # Send length of manifest first (4 bytes)
            writer.write(len(manifest_bytes).to_bytes(4, byteorder='big'))
            writer.write(manifest_bytes)
            await writer.drain()
            
            total_data_size = sum(file['size'] for file in manifest['files'])
            if total_data_size == 0:
                self._update_progress("Manifest", 0, 0)
                return

            # 2. Iterate and Stream each file
            for file_info in manifest['files']:
                rel_path = file_info['rel_path']
                file_size = file_info['size']
                abs_path = source_dir / rel_path

                bytes_sent = 0
                while bytes_sent < file_size:
                    chunk_to_read = min(self.CHUNK_SIZE, file_size - bytes_sent)
                    chunk_data = await self._read_file_chunk(abs_path, bytes_sent, chunk_to_read)
                    
                    if not chunk_data:
                        raise StreamEngineError(f"Unexpected EOF reading file: {rel_path}")

                    # Write raw chunk data directly to TCP socket stream
                    writer.write(chunk_data)
                    await writer.drain()
                    
                    bytes_sent += len(chunk_data)
                    self._update_progress(rel_path, len(chunk_data), total_data_size)

        except (ConnectionError, OSError) as e:
            self._update_progress("Network", 0, 1, error=f"Network disconnected: {e}")
            raise StreamEngineError(f"Network error during folder stream out: {e}")
        except Exception as e:
            self._update_progress("System", 0, 1, error=str(e))
            raise StreamEngineError(f"Error during folder stream out: {e}")

    async def stream_folder_in(
        self, 
        dest_dir: Path, 
        reader: asyncio.StreamReader
    ) -> None:
        """Receives a folder structure and streams file chunks from TCP."""
        self._start_time = time.time()
        self._transferred_bytes = 0

        try:
            # 1. Read Manifest length (4 bytes)
            len_bytes = await reader.readexactly(4)
            manifest_len = int.from_bytes(len_bytes, byteorder='big')
            
            # Read Manifest JSON
            manifest_bytes = await reader.readexactly(manifest_len)
            manifest = json.loads(manifest_bytes.decode('utf-8'))
            
            # Reconstruct directories
            FolderSerializer.reconstruct_structure(dest_dir, manifest)
            
            total_data_size = sum(file['size'] for file in manifest['files'])
            if total_data_size == 0:
                self._update_progress("Finished", 0, 0)
                return

            # 2. Stream all files
            for file_info in manifest['files']:
                rel_path = file_info['rel_path']
                file_size = file_info['size']
                abs_path = dest_dir / rel_path

                # Create empty file first to ensure write handles correctly
                if not abs_path.exists():
                    abs_path.parent.mkdir(parents=True, exist_ok=True)
                    abs_path.write_bytes(b'')

                bytes_received = 0
                while bytes_received < file_size:
                    chunk_to_read = min(self.CHUNK_SIZE, file_size - bytes_received)
                    
                    # Read binary chunk from TCP socket stream
                    chunk_data = await reader.readexactly(chunk_to_read)
                    if not chunk_data:
                        raise StreamEngineError(f"Unexpected socket closed for file: {rel_path}")

                    # Write chunk asynchronously to local file
                    await self._write_file_chunk(abs_path, bytes_received, chunk_data)
                    
                    bytes_received += len(chunk_data)
                    self._update_progress(rel_path, len(chunk_data), total_data_size)

        except asyncio.IncompleteReadError as e:
            self._update_progress("Network", 0, 1, error="Network connection interrupted mid-transfer")
            raise StreamEngineError("Connection interrupted mid-transfer (IncompleteReadError)")
        except (ConnectionError, OSError) as e:
            self._update_progress("Network", 0, 1, error=f"Network failure: {e}")
            raise StreamEngineError(f"Network error during folder stream in: {e}")
        except Exception as e:
            self._update_progress("System", 0, 1, error=str(e))
            raise StreamEngineError(f"Error during folder stream in: {e}")


# TCP helper engines for hosting or connecting
class P2PStreamSender:
    """TCP Server/Client wrapper to send files and track speed."""
    
    def __init__(self, host: str, port: int, progress_callback: Optional[Callable[[TransferProgress], None]] = None):
        self.host = host
        self.port = port
        self.streamer = ChunkedStreamer(progress_callback)

    async def send_directory(self, source_dir: Path) -> None:
        """Connects to the receiver and streams the directory contents."""
        writer = None
        try:
            reader, writer = await asyncio.open_connection(self.host, self.port)
            await self.streamer.stream_folder_out(source_dir, writer)
        finally:
            if writer:
                writer.close()
                await writer.wait_closed()


class P2PStreamReceiver:
    """TCP Server/Client wrapper to receive files and reconstruct directory structure."""

    def __init__(self, port: int, dest_dir: Path, progress_callback: Optional[Callable[[TransferProgress], None]] = None):
        self.port = port
        self.dest_dir = Path(dest_dir)
        self.streamer = ChunkedStreamer(progress_callback)
        self._server = None

    async def start(self) -> None:
        """Starts a TCP server to listen for incoming directory streams."""
        self._server = await asyncio.start_server(
            self._handle_client, 
            '0.0.0.0', 
            self.port
        )
        # Runs in background
        async with self._server:
            await self._server.serve_forever()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self.streamer.stream_folder_in(self.dest_dir, reader)
        except Exception as e:
            print(f"[Receiver Error] {e}")
        finally:
            writer.close()
            await writer.wait_closed()

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
