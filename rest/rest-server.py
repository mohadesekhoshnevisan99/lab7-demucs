#!/usr/bin/env python3
"""Lab 7 REST API for music separation.

Development defaults use localhost Redis and MinIO. When running in Kubernetes,
configure REDIS_HOST, MINIO_ENDPOINT, and REST_BIND_HOST through environment variables.
"""

import base64
import binascii
import hashlib
import io
import json
import os
import re

import redis
from flask import Flask, jsonify, request, send_file
from minio import Minio
from minio.error import S3Error

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

QUEUE_NAME = os.getenv("QUEUE_NAME", "toWorkers")
INPUT_BUCKET = "queue"
OUTPUT_BUCKET = "output"
VALID_TRACKS = {"bass.mp3", "vocals.mp3", "drums.mp3", "other.mp3"}

redis_client = redis.Redis(
    host=os.getenv("REDIS_HOST", "127.0.0.1"),
    port=int(os.getenv("REDIS_PORT", "6379")),
    decode_responses=True,
    socket_connect_timeout=2,
    socket_timeout=2,
)

minio_client = Minio(
    os.getenv("MINIO_ENDPOINT", "127.0.0.1:9000"),
    access_key=os.environ["MINIO_ACCESS_KEY"],
    secret_key=os.environ["MINIO_SECRET_KEY"],
    secure=os.getenv("MINIO_SECURE", "false").lower() == "true",
)


def validate_output_path(songhash, track):
    """Return the MinIO output object name, or a useful 400 response."""
    if not re.fullmatch(r"[0-9a-f]{64}", songhash):
        return None, (jsonify({"error": "Invalid song hash"}), 400)

    # The REST README says 'base.mp3' in one place; Demucs produces 'bass.mp3'.
    # Accept the former as an alias for compatibility with the handout.
    canonical_track = "bass.mp3" if track == "base.mp3" else track
    if canonical_track not in VALID_TRACKS:
        return None, (jsonify({"error": "Invalid track name"}), 400)

    part = canonical_track.removesuffix(".mp3")
    return f"{songhash}-{part}.mp3", None


def minio_error_response(error):
    if error.code in ("NoSuchKey", "NoSuchObject", "NoSuchBucket"):
        return jsonify({"error": "Track not found"}), 404
    app.logger.exception("MinIO request failed")
    return jsonify({"error": "Object storage unavailable"}), 503


@app.route("/", methods=["GET"])
def home():
    return jsonify({"message": "Music Separation Server", "status": "ok"}), 200


@app.route("/apiv1/queue", methods=["GET"])
@app.route("/apiv1/queue/", methods=["GET"])
def get_queue():
    try:
        jobs = redis_client.lrange(QUEUE_NAME, 0, -1)
        hashes = []
        for raw in jobs:
            try:
                job = json.loads(raw)
                hashes.append(job.get("hash", raw) if isinstance(job, dict) else raw)
            except (ValueError, TypeError):
                hashes.append(raw)
        return jsonify({"queue": hashes}), 200
    except redis.exceptions.RedisError:
        app.logger.exception("Redis queue lookup failed")
        return jsonify({"error": "Cannot connect to Redis"}), 503


@app.route("/apiv1/separate", methods=["POST"])
def separate():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or "mp3" not in data:
        return jsonify({"error": "Missing mp3 data"}), 400

    try:
        mp3_bytes = base64.b64decode(data["mp3"], validate=True)
    except (ValueError, TypeError, binascii.Error):
        return jsonify({"error": "Invalid base64 data"}), 400
    if not mp3_bytes:
        return jsonify({"error": "Empty MP3 file"}), 400

    songhash = hashlib.sha256(mp3_bytes).hexdigest()
    filename = f"{songhash}.mp3"
    try:
        minio_client.put_object(
            INPUT_BUCKET,
            filename,
            io.BytesIO(mp3_bytes),
            length=len(mp3_bytes),
            content_type="audio/mpeg",
        )
        job = {
            "hash": songhash,
            "bucket": INPUT_BUCKET,
            "filename": filename,
            "model": data.get("model") or "htdemucs",
            "callback": data.get("callback"),
        }
        redis_client.lpush(QUEUE_NAME, json.dumps(job))
        return jsonify({
            "hash": songhash,
            "reason": "Song enqueued for separation",
        }), 200
    except Exception:
        app.logger.exception("Could not store MP3 or enqueue job")
        return jsonify({"error": "Unable to store or enqueue song"}), 503


@app.route("/apiv1/track/<songhash>/<track>", methods=["GET"])
def get_track(songhash, track):
    object_name, error = validate_output_path(songhash, track)
    if error is not None:
        return error
    try:
        response = minio_client.get_object(OUTPUT_BUCKET, object_name)
        try:
            track_bytes = response.read()
        finally:
            response.close()
            response.release_conn()
    except S3Error as exc:
        return minio_error_response(exc)
    except Exception:
        app.logger.exception("Could not download track from MinIO")
        return jsonify({"error": "Object storage unavailable"}), 503

    return send_file(
        io.BytesIO(track_bytes),
        mimetype="audio/mpeg",
        as_attachment=True,
        download_name=object_name,
    )


@app.route("/apiv1/remove/<songhash>/<track>", methods=["GET", "DELETE"])
def remove_track(songhash, track):
    object_name, error = validate_output_path(songhash, track)
    if error is not None:
        return error
    try:
        # MinIO removal is idempotent; check existence to report missing tracks.
        minio_client.stat_object(OUTPUT_BUCKET, object_name)
        minio_client.remove_object(OUTPUT_BUCKET, object_name)
    except S3Error as exc:
        return minio_error_response(exc)
    except Exception:
        app.logger.exception("Could not remove track from MinIO")
        return jsonify({"error": "Object storage unavailable"}), 503

    return jsonify({"hash": songhash, "track": object_name, "reason": "Track removed"}), 200


if __name__ == "__main__":
    app.run(host=os.getenv("REST_BIND_HOST", "127.0.0.1"), port=int(os.getenv("REST_PORT", "5000")))
