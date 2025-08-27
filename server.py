import os
import sys
import subprocess
import requests
from pathlib import Path
from dotenv import load_dotenv
from flask import Flask, request, jsonify

env_file = Path("/app/env/.env")
if env_file.exists():
  print(f"🔧 Sourcing {env_file}...")
  load_dotenv(env_file)
else:
  print(f"No .env file found at {env_file}. Exiting.")
  sys.exit(0)

IMAGE = "ghcr.io/electron-minecraft-launcher/eml-admintool"
ENV = os.getenv("ENVIRONMENT", "production")
TOKEN = os.getenv("UPDATER_HTTP_API_TOKEN")

app = Flask(__name__)

def get_latest_release():
  url = "https://api.github.com/repos/Electron-Minecraft-Launcher/EML-AdminTool-v2/releases/latest"
  try:
    response = requests.get(url, timeout=10)
    if response.status_code == 200:
      data = response.json()
    else:
      print(f"Failed to fetch latest release: {response.status_code} {response.text}")
      return None
  except Exception as e:
    print(f"Error fetching latest release: {e}")
    return None

  tag_name = data.get("tag_name", "").lstrip("v")
  published_at = data.get("published_at", "").split("T")[0]
  changelogs = data.get("body", "")
  assets = data.get("assets", [])

  compose_url = None
  for asset in assets:
    if asset["name"] == "docker-compose.prod.yml":
      compose_url = asset["browser_download_url"]
      break

  return {
      "tag_name": tag_name,
      "published_at": published_at,
      "changelogs": changelogs,
      "compose_url": compose_url
  }

def update_compose_file(compose_url, dest_path):
  try:
    resp = requests.get(compose_url, timeout=10)
    resp.raise_for_status()
    Path(dest_path).write_bytes(resp.content)
    print(f"✅ New docker-compose.prod.yml saved to {dest_path}")
    return True
  except Exception as e:
    print(f"❌ Unable to download docker-compose: {e}")
    return False

@app.route("/update", methods=["POST"])
def update():
  auth = request.headers.get("Authorization")
  if auth != f"Bearer {TOKEN}":
    print("Unauthorized access attempt")
    return jsonify({"success": False, "error": "Unauthorized"}), 401

  release_info = get_latest_release()
  if not release_info:
    return jsonify({"success": False, "error": "Unable to fetch release"}), 500

  if ENV == "development":
    print("🔧 Mock update: nothing is done.")
    return jsonify({"success": True, "message": f"Mock update to {release_info['tag_name']} successful"})

  if release_info["compose_url"]:
      compose_ok = update_compose_file(release_info["compose_url"], "/app/compose/docker-compose.prod.yml")
      if not compose_ok:
          return jsonify({"success": False, "error": "Failed to download compose"}), 500

  try:
    print(f"Pull {IMAGE}:{release_info['tag_name']}...")
    subprocess.check_call(["docker", "pull", f"{IMAGE}:{release_info['tag_name']}"])
    print("🔄 Restarting the web service...")
    subprocess.check_call(["docker", "compose", "-f", "/app/compose/docker-compose.prod.yml", "up", "-d"])
    return jsonify({"success": True, "message": "Update applied"})
  except subprocess.CalledProcessError as e:
    print(f"❌ Error during update: {e}")
    return jsonify({"success": False, "error": "See logs"}), 500

# async function
