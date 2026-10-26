#!/usr/bin/env python3
"""Isolated unit tests for Lab 7 track/remove REST routes.

These tests MOCK MinIO for the output tracks. They do NOT prove that Demucs
created real audio files, and they do NOT delete anything from your MinIO bucket.
"""
import importlib.util
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch

script = Path(__file__).resolve().parent / "rest" / "rest-server.py"
spec = importlib.util.spec_from_file_location("lab7_rest_server", script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

app = module.app
client = app.test_client()
songhash = "a" * 64

# Keep this a synthetic byte string: it is NOT a generated music track.
example_bytes = b"unit-test-data-not-an-mp3"
obj = Mock()
obj.read.return_value = example_bytes
with patch.object(module.minio_client, "get_object", return_value=obj) as get_object:
    response = client.get(f"/apiv1/track/{songhash}/vocals.mp3")
    assert response.status_code == 200, response.get_data(as_text=True)
    assert response.data == example_bytes
    assert response.mimetype == "audio/mpeg"
    assert "attachment" in response.headers["Content-Disposition"]
    get_object.assert_called_once_with("output", f"{songhash}-vocals.mp3")
print("PASS: track endpoint downloads bytes")

with patch.object(module.minio_client, "stat_object") as stat_object, \
     patch.object(module.minio_client, "remove_object") as remove_object:
    response = client.get(f"/apiv1/remove/{songhash}/drums.mp3")
    assert response.status_code == 200, response.get_data(as_text=True)
    stat_object.assert_called_once_with("output", f"{songhash}-drums.mp3")
    remove_object.assert_called_once_with("output", f"{songhash}-drums.mp3")
print("PASS: remove endpoint targets correct object")

assert client.get(f"/apiv1/track/{songhash}/bad.mp3").status_code == 400
assert client.get("/apiv1/track/not-a-hash/vocals.mp3").status_code == 400
print("PASS: invalid track and hash are rejected")
print("ALL REST TRACK/REMOVE UNIT TESTS PASSED (mocked MinIO)")
