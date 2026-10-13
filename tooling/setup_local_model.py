"""Pinned official artifacts, hash verified; all bytes stay on D drive."""
import hashlib
import json
import os
import subprocess
import zipfile
import argparse
from pathlib import Path

import httpx

CACHE = Path("D:/CodexWork/cache/sentinellab-model")
ROOT = Path(__file__).resolve().parents[1]
TAG = "b11577"
MODELS = {"0.6B": ("23749fefcc72300e3a2ad315e1317431b06b590a", "Qwen3-0.6B-Q8_0.gguf"),
          "1.7B": ("90862c4b9d2787eaed51d12237eafdfe7c5f6077", "Qwen3-1.7B-Q8_0.gguf"),
          "4B": ("bc640142c66e1fdd12af0bd68f40445458f3869b", "Qwen3-4B-Q4_K_M.gguf")}


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(client, url, target, expected):
    if target.exists() and sha256(target) == expected:
        return
    partial = target.with_suffix(target.suffix + ".partial")
    with client.stream("GET", url) as response:
        response.raise_for_status()
        with partial.open("wb") as f:
            size = 0
            milestone = 0
            for chunk in response.iter_bytes(1024 * 1024):
                f.write(chunk)
                size += len(chunk)
                if size // (100 * 1024 * 1024) > milestone:
                    milestone = size // (100 * 1024 * 1024)
                    print(f"{target.name}: {size // (1024 * 1024)} MiB", flush=True)
    if sha256(partial) != expected:
        raise RuntimeError("artifact hash differs from upstream metadata")
    partial.replace(target)


def main(size="4B", backend="vulkan"):
    model_revision, model_file = MODELS[size]
    model_repo = "Qwen/Qwen3-" + size + "-GGUF"
    CACHE.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["git", "credential-manager", "get"], input="protocol=https\nhost=github.com\n\n", text=True,
                            capture_output=True, env={**os.environ, "GCM_INTERACTIVE": "never"})
    credential = dict(s.split("=", 1) for s in result.stdout.splitlines() if "=" in s)
    headers = {"Authorization": "Bearer " + credential["password"]} if credential.get("password") else {}
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        release = client.get(f"https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/{TAG}", headers=headers)
        release.raise_for_status()
        asset = next(a for a in release.json()["assets"] if a["name"].endswith("bin-win-" + backend + "-x64.zip"))
        runtime_digest = asset["digest"].removeprefix("sha256:")
        tree = client.get(f"https://huggingface.co/api/models/{model_repo}/tree/{model_revision}")
        tree.raise_for_status()
        model = next(f for f in tree.json() if f["path"] == model_file)
        model_digest = model["lfs"]["oid"]
        archive = CACHE / asset["name"]
        download(client, asset["browser_download_url"], archive, runtime_digest)
        model_path = CACHE / model_file
        download(client, f"https://huggingface.co/{model_repo}/resolve/{model_revision}/{model_file}", model_path, model_digest)
    runtime = CACHE / (TAG + "-" + backend)
    runtime.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            resolved = (runtime / member.filename).resolve()
            if not resolved.is_relative_to(runtime.resolve()):
                raise RuntimeError("archive contains an unsafe path")
        bundle.extractall(runtime)
    server = next(runtime.rglob("llama-server.exe"))
    manifest = {"runtime_tag": TAG, "runtime_sha256": runtime_digest, "backend": backend, "model": model_file.removesuffix(".gguf"),
                "model_revision": model_revision, "model_sha256": model_digest, "model_path": str(model_path), "server_path": str(server),
                "sources": ["https://github.com/ggml-org/llama.cpp", "https://huggingface.co/" + model_repo]}
    (CACHE / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (ROOT / "artifacts" / "model-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("Verified local model and runtime ready on D drive", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--size", choices=MODELS, default="4B")
    parser.add_argument("--backend", choices=["cpu", "vulkan"], default="vulkan")
    args=parser.parse_args()
    main(args.size,args.backend)
