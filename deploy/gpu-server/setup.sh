#!/usr/bin/env bash
# Install ComfyUI on an Ubuntu GPU rental. Run as the intended service account.
set -euo pipefail

usage() {
    cat <<'HELP'
Usage: bash deploy/gpu-server/setup.sh [--dry-run] [--help]

Environment:
  COMFYUI_DIR           Checkout and venv directory (default: $HOME/ComfyUI).
  COMFYUI_REPO          HTTPS Git repository (default: Comfy-Org/ComfyUI).
  COMFYUI_REF           Optional Git tag, branch or commit; existing code is otherwise kept.
  COMFYUI_PORT          Loopback port (default: 8188).
  PYTORCH_INDEX_URL     CUDA wheel index (default: https://download.pytorch.org/whl/cu130).
  CUSTOM_NODES          Space/newline-separated HTTPS Git URLs (default: empty).
  MODELS_MANIFEST       YAML manifest (default: models.yaml beside this script).
  MODEL_PROFILE         none (default), draft12, balanced24 or quality48; opt-in downloads.
  HF_TOKEN              Optional Hugging Face token, read only from the environment.
  SERVICE_MODE          auto, systemd or tmux (default: auto).
  DRY_RUN               1 prints a plan without installs, downloads or GPU checks.

ComfyUI always listens on 127.0.0.1. Connect through an SSH tunnel.
HELP
}

DRY_RUN="${DRY_RUN:-0}"
for argument in "$@"; do
    case "$argument" in
        --help|-h) usage; exit 0 ;;
        --dry-run) DRY_RUN=1 ;;
        *) printf 'Unknown argument: %s\n' "$argument" >&2; exit 2 ;;
    esac
done

fail() { printf 'setup: %s\n' "$*" >&2; exit 1; }
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
COMFYUI_DIR="${COMFYUI_DIR:-${HOME}/ComfyUI}"
COMFYUI_REPO="${COMFYUI_REPO:-https://github.com/Comfy-Org/ComfyUI.git}"
COMFYUI_REF="${COMFYUI_REF:-}"
COMFYUI_PORT="${COMFYUI_PORT:-8188}"
# Current ComfyUI requires CUDA 13.0+ wheels; this meets the Blackwell CUDA 12.8+ baseline.
PYTORCH_INDEX_URL="${PYTORCH_INDEX_URL:-https://download.pytorch.org/whl/cu130}"
CUSTOM_NODES="${CUSTOM_NODES:-}"
MODELS_MANIFEST="${MODELS_MANIFEST:-${SCRIPT_DIR}/models.yaml}"
MODEL_PROFILE="${MODEL_PROFILE:-none}"
SERVICE_MODE="${SERVICE_MODE:-auto}"
[[ "$MODEL_PROFILE" =~ ^(none|draft12|balanced24|quality48)$ ]] || fail 'MODEL_PROFILE must be none, draft12, balanced24 or quality48.'
[[ "$DRY_RUN" == 0 || "$DRY_RUN" == 1 ]] || fail 'DRY_RUN must be 0 or 1.'
[[ "$SERVICE_MODE" =~ ^(auto|systemd|tmux)$ ]] || fail 'SERVICE_MODE must be auto, systemd or tmux.'
# Simple absolute paths are also safe in systemd unit syntax and tmux commands.
[[ "$COMFYUI_DIR" =~ ^/[a-zA-Z0-9_./-]+$ ]] || fail 'COMFYUI_DIR must be an absolute path without spaces or shell metacharacters.'
[[ "$COMFYUI_PORT" =~ ^[0-9]{1,5}$ ]] || fail 'COMFYUI_PORT must be a port number.'
(( 10#$COMFYUI_PORT >= 1 && 10#$COMFYUI_PORT <= 65535 )) || fail 'COMFYUI_PORT must be between 1 and 65535.'
COMFYUI_PORT=$((10#$COMFYUI_PORT))
[[ "$COMFYUI_REPO" == https://* ]] || fail 'COMFYUI_REPO must use HTTPS.'
[[ "$PYTORCH_INDEX_URL" == https://* ]] || fail 'PYTORCH_INDEX_URL must use HTTPS.'

if [[ "$DRY_RUN" == 1 ]]; then
    printf 'DRY_RUN: no files will be changed and no network/GPU calls will be made.\n'
    printf 'Check NVIDIA driver; install Python/venv, FFmpeg, Git and tmux if apt-get is available.\n'
    printf 'Prepare ComfyUI and its venv at %s; install CUDA PyTorch and requested custom nodes.\n' "$COMFYUI_DIR"
    printf 'Model profile %s from %s (none downloads no weights); preserve and verify existing models.\n' "$MODEL_PROFILE" "$MODELS_MANIFEST"
    printf 'Start ComfyUI with %s on 127.0.0.1:%s.\n' "$SERVICE_MODE" "$COMFYUI_PORT"
    exit 0
fi

command -v nvidia-smi >/dev/null 2>&1 || fail 'nvidia-smi is missing. Select an NVIDIA GPU image and install its host driver before setup. Use --dry-run to preview without a GPU.'
nvidia-smi || fail 'The NVIDIA driver is not working. Repair the rental image/driver first.'

SUDO=()
if (( EUID != 0 )); then
    command -v sudo >/dev/null 2>&1 || fail 'sudo is required for Ubuntu packages and the systemd unit.'
    SUDO=(sudo)
fi
if command -v apt-get >/dev/null 2>&1; then
    "${SUDO[@]}" apt-get update
    "${SUDO[@]}" env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        python3 python3-venv ffmpeg git tmux ca-certificates
else
    printf 'apt-get unavailable; using preinstalled Python, FFmpeg, Git and tmux.\n'
fi
for executable in python3 ffmpeg git; do
    command -v "$executable" >/dev/null 2>&1 || fail "Required command missing: $executable"
done
python3 -c 'import sys; sys.exit("Python 3.11+ is required; use Ubuntu 24.04 or a newer Python image.") if sys.version_info < (3, 11) else None'

clone_if_missing() {
    local repository="$1" destination="$2"
    if [[ -d "$destination/.git" ]]; then
        printf 'Keeping existing checkout: %s\n' "$destination"
    elif [[ -e "$destination" ]]; then
        fail "Existing destination is not a Git checkout: $destination"
    else
        git clone --depth 1 -- "$repository" "$destination"
    fi
}

mkdir -p -- "$(dirname -- "$COMFYUI_DIR")"
clone_if_missing "$COMFYUI_REPO" "$COMFYUI_DIR"
if [[ -n "$COMFYUI_REF" ]]; then
    [[ -z "$(git -C "$COMFYUI_DIR" status --porcelain --untracked-files=no)" ]] || fail 'Commit or stash ComfyUI changes before selecting COMFYUI_REF.'
    git -C "$COMFYUI_DIR" fetch --depth 1 origin "$COMFYUI_REF"
    git -C "$COMFYUI_DIR" checkout --detach FETCH_HEAD
fi
if [[ ! -x "$COMFYUI_DIR/.venv/bin/python" ]]; then
    python3 -m venv "$COMFYUI_DIR/.venv"
fi
COMFY_PYTHON="$COMFYUI_DIR/.venv/bin/python"
"$COMFY_PYTHON" -m pip install --upgrade torch torchvision torchaudio --index-url "$PYTORCH_INDEX_URL"
"$COMFY_PYTHON" -m pip install -r "$COMFYUI_DIR/requirements.txt" \
    --extra-index-url "$PYTORCH_INDEX_URL" 'PyYAML>=6'

mkdir -p -- "$COMFYUI_DIR/custom_nodes"
CUSTOM_NODES="${CUSTOM_NODES//$'\n'/ }"
read -r -a node_repositories <<< "$CUSTOM_NODES"
for repository in "${node_repositories[@]}"; do
    [[ "$repository" == https://* ]] || fail 'Custom node repositories must use HTTPS.'
    name="${repository##*/}"
    name="${name%.git}"
    [[ "$name" =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.-]*$ ]] || fail "Invalid custom node repository name: $name"
    destination="$COMFYUI_DIR/custom_nodes/$name"
    clone_if_missing "$repository" "$destination"
    if [[ -f "$destination/requirements.txt" ]]; then
        "$COMFY_PYTHON" -m pip install -r "$destination/requirements.txt" \
            --extra-index-url "$PYTORCH_INDEX_URL"
    fi
done

"$COMFY_PYTHON" "$SCRIPT_DIR/download_models.py" "$MODELS_MANIFEST" "$COMFYUI_DIR/models" "$MODEL_PROFILE"

# Smoke only the local device, after every extension has installed its requirements.
"$COMFY_PYTHON" -c 'import torch; assert torch.cuda.is_available(), "PyTorch cannot use the GPU; check driver and PYTORCH_INDEX_URL"; print(torch.cuda.get_device_name(0)); print(torch.ones(1, device="cuda") + 1)'

if [[ "$SERVICE_MODE" == auto ]]; then
    if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
        SERVICE_MODE=systemd
    else
        SERVICE_MODE=tmux
    fi
fi
if [[ "$SERVICE_MODE" == systemd ]]; then
    command -v systemctl >/dev/null 2>&1 || fail 'systemctl missing; use SERVICE_MODE=tmux.'
    [[ -d /run/systemd/system ]] || fail 'systemd is not running; use SERVICE_MODE=tmux.'
    service_user="$(id -un)"
    "${SUDO[@]}" tee /etc/systemd/system/avd-comfyui.service >/dev/null <<UNIT
[Unit]
Description=AI Video Director ComfyUI (SSH access only)
After=network.target

[Service]
Type=simple
User=$service_user
WorkingDirectory=$COMFYUI_DIR
ExecStart=$COMFY_PYTHON $COMFYUI_DIR/main.py --listen 127.0.0.1 --port $COMFYUI_PORT
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
UMask=0077

[Install]
WantedBy=multi-user.target
UNIT
    "${SUDO[@]}" systemctl daemon-reload
    "${SUDO[@]}" systemctl enable avd-comfyui.service
    "${SUDO[@]}" systemctl restart avd-comfyui.service
    "${SUDO[@]}" systemctl is-active --quiet avd-comfyui.service
else
    command -v tmux >/dev/null 2>&1 || fail 'tmux missing; install it or use systemd.'
    if tmux has-session -t avd-comfyui 2>/dev/null; then
        printf 'Keeping existing tmux session avd-comfyui; restart it after changing settings.\n'
    else
        # Do not leave the download token in the long-running tmux process environment.
        env -u HF_TOKEN tmux new-session -d -s avd-comfyui -c "$COMFYUI_DIR" \
            "exec env -u HF_TOKEN $COMFY_PYTHON $COMFYUI_DIR/main.py --listen 127.0.0.1 --port $COMFYUI_PORT"
    fi
fi
printf 'ComfyUI configured on 127.0.0.1:%s. Check startup logs before submitting work.\n' "$COMFYUI_PORT"
