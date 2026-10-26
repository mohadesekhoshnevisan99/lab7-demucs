#!/usr/bin/env python3
"""Lab 7: test the worker's job-processing flow without running Demucs.

- Reads, but does not remove, the oldest real job in Redis.
- Reads and verifies the real MP3 stored in MinIO.
- Simulates the Demucs CLI output with four placeholder files in a temp folder.
- Intercepts MinIO output uploads, Redis logs and HTTP callbacks.
- DOES NOT create output objects, run Demucs, or consume queue entries.

This is a MOCKED orchestration test, not a real music-separation test.
Run from the repository root with MinIO env vars sourced:
    python test_worker_pipeline.py
"""

import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock, patch

WORKER_PATH = Path(__file__).resolve().parent / "worker" / "worker-server.py"
spec = importlib.util.spec_from_file_location("lab7_worker", WORKER_PATH)
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)

raw = worker.redis_client.lindex(worker.QUEUE_NAME, -1)
if raw is None:
    raise SystemExit("No job in Redis. Submit a short MP3 before running this test.")
job = json.loads(raw)
queue_before = worker.redis_client.lrange(worker.QUEUE_NAME, 0, -1)
upload_calls = []
command_calls = []


def fake_demucs(command, check):
    assert check is True
    assert command[:3] == [worker.sys.executable, "-m", "demucs.separate"]
    assert "--mp3" in command
    assert "--out" in command
    assert "--name" in command
    output_dir = Path(command[command.index("--out") + 1])
    model = command[command.index("--name") + 1]
    source_path = Path(command[-1])
    assert source_path.is_file()
    songhash = job["hash"]
    assert source_path.name == f"{songhash}.mp3"
    for part in worker.TRACKS:
        folder = output_dir / model / songhash
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{part}.mp3").write_bytes(b"FAKE TEST TRACK - NOT MUSIC")
    command_calls.append(command)
    return Mock(returncode=0)


def fake_upload(bucket, object_name, path, content_type):
    assert bucket == "output"
    assert content_type == "audio/mpeg"
    assert Path(path).read_bytes() == b"FAKE TEST TRACK - NOT MUSIC"
    upload_calls.append(object_name)


with patch.object(worker.subprocess, "run", side_effect=fake_demucs), \
     patch.object(worker.minio_client, "fput_object", side_effect=fake_upload), \
     patch.object(worker.requests, "post", return_value=Mock(status_code=200)), \
     patch.object(worker, "log", side_effect=lambda message: print("[mock log]", message)):
    worker.process_job(job)

expected = {f"{job['hash']}-{part}.mp3" for part in worker.TRACKS}
assert len(command_calls) == 1, "Expected exactly one Demucs invocation"
assert set(upload_calls) == expected, (upload_calls, expected)
assert len(upload_calls) == 4
assert worker.redis_client.lrange(worker.QUEUE_NAME, 0, -1) == queue_before, \
    "The Redis queue changed during a non-destructive test!"

print("PASS: Real MP3 downloaded and SHA-256 verified")
print("PASS: Demucs invocation arguments checked (Demucs NOT actually run)")
print("PASS: Four generated track paths and mocked uploads verified")
print("PASS: The Redis queue was unchanged")
print("PASS: No output objects uploaded; no real callback sent")
print("WORKER PIPELINE ORCHESTRATION TEST PASSED (MOCKED DEMUCS)")
