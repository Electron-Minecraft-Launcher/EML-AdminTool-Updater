"""
EML AdminTool Updater server
"""
__license__ = "MIT"
__author__ = "GoldFrite"
__version__ = "1.1.0"

import os
import sys
import requests
import asyncio
import threading
import json
import socket
import re
from pathlib import Path
from dotenv import load_dotenv
from flask import Flask, request, jsonify

env_file = Path("/app/env/.env")
if env_file.exists():
  print(f"🔧 Sourcing {env_file}...")
  load_dotenv(env_file)
  print("🔧 Done.")
else:
  print(f"No .env file found at {env_file}. Exiting.")
  sys.exit(0)

IMAGE = "ghcr.io/electron-minecraft-launcher/eml-admintool"
ENV = os.getenv("ENVIRONMENT", "production")
TOKEN = os.getenv("UPDATER_HTTP_API_TOKEN")
API_URL = os.getenv(
    "UPDATER_API_URL", "https://api.github.com/repos/Electron-Minecraft-Launcher/EML-AdminTool/releases/latest")

app = Flask(__name__)

def get_latest_release():
  try:
    response = requests.get(API_URL, timeout=10)
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
    print(f"❌ Unable to download docker-compose file: {e}")
    return False

async def run_cmd(*args):
  proc = await asyncio.create_subprocess_exec(
      *args,
      stdout=asyncio.subprocess.PIPE,
      stderr=asyncio.subprocess.PIPE
  )
  stdout, stderr = await proc.communicate()
  return proc.returncode, stdout.decode(), stderr.decode()

async def download_update():
  try:
    container_id = socket.gethostname()

    code, out, err = await run_cmd("docker", "inspect", container_id)
    if code != 0:
      print("Failed to inspect self")
      return

    inspect_data = json.loads(out)[0]
    labels = inspect_data["Config"]["Labels"]

    project_name = labels.get("com.docker.compose.project", "eml-admintool")
    host_dir = labels.get("com.docker.compose.project.working_dir")
    image_name = inspect_data["Config"]["Image"]

    if not host_dir:
      print("Error: Could not determine host working directory from labels.")
      return

    agent_workdir = host_dir
    if re.match(r'^[a-zA-Z]:\\', host_dir):
      drive = host_dir[0].lower()
      path_part = host_dir[3:].replace('\\', '/')
      agent_workdir = f"/host_mnt/{drive}/{path_part}"

    print("📥 Pulling images from the new compose file...")
    code, out, err = await run_cmd("docker", "compose", "-p", project_name, "-f", "/app/compose/docker-compose.prod.yml", "pull")
    if code != 0:
      print(f"❌ Docker pull failed:\n{err}")
      return

    print(out)

    print("🏗️ Launching ephemeral update agent...")
    update_cmd = [
        "docker", "run", "--rm", "-d",
        "--name", f"{project_name}-agent",
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        "-v", f"{host_dir}:{agent_workdir}",
        "-w", agent_workdir,
        image_name,
        "sh", "-c",
        f"sleep 3 && docker compose -p {project_name} -f docker-compose.prod.yml up -d --remove-orphans"
    ]

    code, out, err = await run_cmd(*update_cmd)
    if code == 0:
      print("🔄️ Update delegated to ephemeral agent...")
    else:
      print(f"Failed to launch agent:\n{err}")

  except Exception as e:
    print(f"Error during update: {e}")

@app.route("/update", methods=["POST"])
def update():
  auth = request.headers.get("Authorization")
  if auth != f"Bearer {TOKEN}":
    print("Unauthorized access attempt")
    return jsonify({"success": False, "error": "Unauthorized"}), 401

  print("🔄 Update requested...")

  release_info = get_latest_release()
  if not release_info:
    return jsonify({"success": False, "error": "Unable to fetch release"}), 500

  if ENV == "development":
    print("🔧 Mock update: nothing is done.")
    return jsonify({"success": True, "message": f"Mock update to {release_info['tag_name']} successful"})

  if release_info["compose_url"]:
    compose_ok = update_compose_file(release_info["compose_url"], "/app/compose/docker-compose.prod.yml")
    if not compose_ok:
      return jsonify({"success": False, "error": "Failed to download compose file"}), 500

  threading.Thread(target=lambda: asyncio.run(download_update()), daemon=True).start()

  return jsonify({"success": True, "message": "Updating..."})

@app.route("/reload", methods=["POST"])
def reload():
  auth_header = request.headers.get("Authorization")
  if not auth_header or not auth_header.startswith("Bearer "):
    return jsonify({"success": False, "error": "Missing token"}), 401

  print("🔄 Reloading environment variables from .env...")
  load_dotenv(env_file, override=True)

  global TOKEN
  TOKEN = os.getenv("UPDATER_HTTP_API_TOKEN")

  if auth_header == f"Bearer {TOKEN}":
    print("✅ Token updated successfully.")
    return jsonify({"success": True, "message": "Updater reloaded successfully"})
  else:
    print("❌ Token mismatch after reload.")
    return jsonify({"success": False, "error": "Token mismatch"}), 403
