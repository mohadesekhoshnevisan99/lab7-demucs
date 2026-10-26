#!/usr/bin/env python3
"""Lab 7 music-separation worker.

--check: inspect the oldest queued song without removing it or running Demucs.
--once: process one job (requires Demucs and enough memory).
No flag: keep working on incoming jobs.
"""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

from minio import Minio
import redis
import requests

QUEUE_NAME = os.getenv("QUEUE_NAME", "toWorkers")
LOG_NAME = "logging"
TRACKS = ("bass", "drums", "vocals", "other")

redis_client = redis.Redis(
    host=os.getenv("REDIS_HOST", "127.0.0.1"),
    port=int(os.getenv("REDIS_PORT", "6379")),
    decode_responses=True,
    socket_connect_timeout=5,
)
minio_client = Minio(
    os.getenv("MINIO_ENDPOINT", "127.0.0.1:9000"),
    access_key=os.environ["MINIO_ACCESS_KEY"],
    secret_key=os.environ["MINIO_SECRET_KEY"],
    secure=os.getenv("MINIO_SECURE", "false").lower() == "true",
)


def log(message):
    print(message, flush=True)
    try:
        redis_client.lpush(LOG_NAME, message)
    except redis.RedisError:
        pass


def download_and_verify(job, target):
    songhash = job["hash"]
    if not re.fullmatch(r"[0-9a-f]{64}", songhash):
        raise ValueError("Unexpected song hash")
    # The REST server stores this MP3 under queue/<songhash>.mp3.
    filename = f"{songhash}.mp3"
    bucket = job.get("bucket", "queue")
    if bucket != "queue" or job.get("filename", filename) != filename:
        raise ValueError("Unexpected input object")

    minio_client.fget_object(bucket, filename, str(target))
    digest = hashlib.sha256()
    with open(target, "rb") as audio:
        for block in iter(lambda: audio.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != songhash:
        raise ValueError("MP3 hash does not match the job")
    return songhash


def process_job(job):
    with tempfile.TemporaryDirectory(prefix="lab7-worker-") as temp_dir:
        temp = Path(temp_dir)
        input_path = temp / f'{job["hash"]}.mp3'
        songhash = download_and_verify(job, input_path)
        log(f"Verified input {songhash} ({input_path.stat().st_size} bytes)")

        model = job.get("model") or "htdemucs"
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", model):
            raise ValueError("Invalid Demucs model name")

        output_dir = temp / "separated"
        command = [
            sys.executable, "-m", "demucs.separate", "--mp3",
            "--name", model, "--out", str(output_dir), str(input_path),
        ]
        log("Running Demucs on " + songhash)
        subprocess.run(command, check=True)

        # Different Demucs models put their results in different subfolders.
        for track in TRACKS:
            matches = list(output_dir.rglob(f"{track}.mp3"))
            if len(matches) != 1:
                raise FileNotFoundError(f"Expected one {track}.mp3, found {len(matches)}")
            object_name = f"{songhash}-{track}.mp3"
            minio_client.fput_object(
                "output", object_name, str(matches[0]),
                content_type="audio/mpeg",
            )
            log(f"Saved output/{object_name}")

        callback = job.get("callback")
        if isinstance(callback, dict) and callback.get("url"):
            try:
                response = requests.post(
                    callback["url"], json=callback.get("data", {}), timeout=10,
                )
                log(f"Callback returned HTTP {response.status_code}")
            except requests.RequestException as exc:
                log(f"Callback failed (non-fatal): {exc}")
        log(f"Finished {songhash}")


def check_oldest_job():
    raw = redis_client.lindex(QUEUE_NAME, -1)
    if raw is None:
        print("No jobs in Redis")
        return
    job = json.loads(raw)
    with tempfile.TemporaryDirectory(prefix="lab7-check-") as temp_dir:
        input_path = Path(temp_dir) / "input.mp3"
        songhash = download_and_verify(job, input_path)
        print(f"Worker input check PASSED: {songhash}")
        print(f"MP3 size: {input_path.stat().st_size} bytes")
        print("Job was NOT removed from Redis; Demucs was NOT run")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="verify oldest job, without consuming it")
    parser.add_argument("--once", action="store_true", help="run Demucs on one queued job, then exit")
    args = parser.parse_args()
    if args.check:
        check_oldest_job()
        return

    if importlib.util.find_spec("demucs") is None:
        raise SystemExit("Demucs isn't installed here. Use --check; run the full worker on a >=6 GB environment.")

    log("Worker listening on " + QUEUE_NAME)
    while True:
        item = redis_client.brpop(QUEUE_NAME, timeout=5)
        if item is None:
            if args.once:
                print("No jobs available")
                return
            continue
        _, raw = item
        try:
            process_job(json.loads(raw))
        except Exception as exc:
            log(f"Worker failed: {type(exc).__name__}: {exc}")
            # Do not permanently discard a job if separation failed.
            redis_client.lpush(QUEUE_NAME, raw)
            if args.once:
                raise
            time.sleep(10)
        if args.once:
            return


if __name__ == "__main__":
    main()
