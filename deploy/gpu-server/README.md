# Rented GPU deployment

Run ComfyUI on an NVIDIA GPU rental and keep the director, project database and media on
your laptop. ComfyUI listens only on `127.0.0.1`; access it through SSH. The app and its
workers need no local GPU when ComfyUI runs remotely.

## Rent and prepare the machine

Choose an Ubuntu 24.04 image with an NVIDIA driver already installed, SSH access, and
enough VRAM and disk for your selected workflows. A 24 GB or 48 GB rental gives more room
than the 12 GB laptop, but check each model's actual requirements before renting. Prefer
persistent storage for large weights and an SSH key. Allow inbound SSH only; do not add
public ports, proxies or provider HTTP endpoints for ComfyUI.

Check the provider's hourly rate, storage charges, minimum billing increment and stop versus
terminate behavior before starting. Keep an external reminder for the rental's end time;
the application budget does not include rental or storage charges.

On the rental, clone this repository and run as the account that should own ComfyUI:

```bash
nvidia-smi
bash deploy/gpu-server/setup.sh --help
DRY_RUN=1 bash deploy/gpu-server/setup.sh
bash deploy/gpu-server/setup.sh
```

The installer uses `sudo` for packages and its system service when run by a non-root user.
It installs Python 3/venv, FFmpeg, Git, tmux and CA certificates through apt, creates
`$HOME/ComfyUI/.venv`, and verifies a real CUDA tensor operation. It does not install an
NVIDIA kernel driver. A missing or broken `nvidia-smi` stops installation with an error;
help and dry-run work without a GPU and perform no downloads or system changes.

The default PyTorch index is `https://download.pytorch.org/whl/cu130`. Current ComfyUI's
manual NVIDIA installation requires CUDA 13.0+ wheels, and PyTorch 2.12 no longer publishes
CUDA 12.8 wheels. CUDA 13.0 satisfies this project's CUDA 12.8+ Blackwell requirement.
The rental's driver must support the chosen wheel runtime. The wheel bundles the CUDA user
libraries; installing a separate CUDA toolkit is not part of this script. Sources checked
October 20, 2026: [ComfyUI installation](https://github.com/Comfy-Org/ComfyUI#manual-install-windows-linux),
[PyTorch 2.12](https://pytorch.org/blog/pytorch-2-12-release-blog/), and
[PyTorch Blackwell introduction](https://pytorch.org/blog/pytorch-2-7/).

Configure installation through environment variables, for example:

```bash
export COMFYUI_DIR="$HOME/ComfyUI"
export PYTORCH_INDEX_URL="https://download.pytorch.org/whl/cu130"
# Optional: pin a revision that you have verified with your nodes/workflows.
# export COMFYUI_REF="YOUR_VERIFIED_COMMIT_OR_TAG"
# export CUSTOM_NODES="https://github.com/OWNER/NODE_ONE.git https://github.com/OWNER/NODE_TWO.git"
bash deploy/gpu-server/setup.sh
```

`COMFYUI_DIR` must be an absolute path without spaces or shell metacharacters.
`COMFYUI_REPO` can select another HTTPS checkout; `COMFYUI_PORT` changes the loopback port.
`CUSTOM_NODES` accepts HTTPS repository URLs separated by spaces or newlines. Review and
trust their code before installation: each node executes Python inside the ComfyUI account.
The installer installs a node's `requirements.txt` when present; node-specific extra steps
are the operator's responsibility.

Re-running setup reuses repositories, the virtual environment and existing verified models.
It does not reset or pull existing checkouts. Set `COMFYUI_REF` to fetch and select a specific
ComfyUI revision; tracked local edits must first be committed or stashed. Package requirements
are rechecked on each run. For repeatable deployments record your ComfyUI/node revisions and
pin their dependencies. Run setup while ComfyUI is idle; the systemd service is restarted.

## Models and service

The canonical manifest is [`models.yaml`](models.yaml) beside the installer. It starts empty
so setup does not silently download large or gated weights. Add entries after checking the
model's license and your workflow's required node and filename:

```yaml
models:
  - name: My reviewed model
    url: https://huggingface.co/OWNER/REPOSITORY/resolve/COMMIT/model.safetensors
    target: diffusion_models
    filename: model.safetensors
    # sha256: paste the publisher's 64-character SHA-256 here
```

`target` is a directory relative to `COMFYUI_DIR/models`. `filename` is optional and defaults
to the URL path's basename. Prefer immutable commit URLs and a publisher-verified `sha256`.
An existing nonempty model without a checksum is reused without verification. With a checksum,
setup verifies an existing file and replaces it only after a successful matching download.
Downloads use temporary files and atomic replacement; failed downloads preserve old models.
No access token belongs in the manifest or a URL.

For gated Hugging Face downloads, accept the model's terms in your account first and provide
a read token interactively. The token is sent only to the original `huggingface.co` request,
never forwarded to redirect hosts or written into the systemd unit:

```bash
read -rsp 'Hugging Face read token: ' HF_TOKEN; printf '\n'
export HF_TOKEN
MODELS_MANIFEST="$PWD/deploy/gpu-server/models.yaml" bash deploy/gpu-server/setup.sh
unset HF_TOKEN
```

`SERVICE_MODE=auto` creates `avd-comfyui.service` when systemd is running; otherwise it starts
the `avd-comfyui` tmux session. You can explicitly set `SERVICE_MODE=systemd` or `tmux`.
Both launch `main.py --listen 127.0.0.1 --port 8188` by default. Check actual startup logs:

```bash
sudo systemctl status avd-comfyui
sudo journalctl -u avd-comfyui -n 100 --no-pager
# For the tmux fallback:
tmux attach -t avd-comfyui
```

Detach from tmux with Ctrl+B then D. A pre-existing tmux session is preserved on rerun; to
apply changed code or settings, stop it and rerun setup. Tmux does not automatically restart
ComfyUI after a crash or reboot. Do not switch service modes while the old process is running.

## Connect from the laptop

Keep this command running in a laptop terminal (add `-p SSH_PORT` for a custom SSH port):

```bash
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -L 127.0.0.1:8188:127.0.0.1:8188 USER@GPU_HOST
```

Open `http://127.0.0.1:8188` locally. If your laptop's local ComfyUI already uses 8188,
change the forwarding argument to `-L 127.0.0.1:8189:127.0.0.1:8188` and set the remote profile to 8189.
Keep the SSH listener on loopback; never add `-g`, `GatewayPorts`, or a wildcard bind address.

In the laptop's repository `.env`, select the remote profile:

```dotenv
AVD_COMFYUI_ACTIVE_PROFILE=remote
AVD_COMFYUI_PROFILES={"local":{"base_url":"http://127.0.0.1:8188","timeout":600},"remote":{"base_url":"http://127.0.0.1:8188","timeout":600}}
```

Restart the app and workers after changing `.env`. The profile name chooses the backend;
the URL reaches the SSH tunnel. Install and verify your real ComfyUI API-format workflows
as described in [`workflows/README.md`](../../workflows/README.md).

## Run the app and workers in Docker

From the repository root, create `.env` from `.env.example`, configure profiles and API keys,
and run:

```bash
docker compose -f deploy/docker-compose.yml up --build -d
docker compose -f deploy/docker-compose.yml logs -f
```

This builds the CPU app image from [`../Dockerfile`](../Dockerfile) and starts the web app
plus one worker for each GPU, API and CPU lane. GPU execution still goes to remote ComfyUI.
Open `http://127.0.0.1:8000`. The app's HTTP healthcheck gates worker startup; it checks the
web process, not whether remote models/workflows are installed or a worker can complete a job.

Compose uses host networking so all processes can reach the laptop's loopback SSH tunnel.
Use Linux, or Docker Desktop with **Enable host networking** selected; Docker documents
[Desktop host networking support and limitations](https://docs.docker.com/engine/network/drivers/host/).
It is not compatible with an arbitrary default bridge setup: a container's `127.0.0.1` would
refer to that container. No port is published publicly. The app explicitly binds `127.0.0.1`.

The image runs as UID 10001 and installs `avd[all]`, FFmpeg and a font for captions.
The named `avd-data` volume holds the shared SQLite database and media, separate from your
host's `./data`. Workflows are mounted read-only; `.env` is read at runtime and excluded by
the Dockerfile's explicit source copies. Environment credentials remain visible to users
with Docker administration access. Keep that access restricted.

Do not scale the GPU worker. The queue serializes GPU jobs, and a single GPU worker avoids
competing processes. Containers get a ten-minute graceful stop window for current jobs;
wait for jobs to finish before stopping long generations. Stop the app without deleting data:

```bash
docker compose -f deploy/docker-compose.yml down
```

Do not add `--volumes` unless you intend to delete the stored projects. Back up the named
volume while all app/worker processes are stopped; a filesystem copy of a live SQLite store
is not a reliable backup.

## Finish and stop billing

Wait for active jobs, copy any needed outputs and configuration, then stop the remote process:

```bash
sudo systemctl disable --now avd-comfyui
# Or, if using the tmux fallback:
tmux kill-session -t avd-comfyui
```

Close the SSH tunnel. **Stop or terminate the GPU instance in the rental provider's console
and confirm its billed state.** Stopping ComfyUI, closing SSH, or shutting down the laptop does
not stop the rental. Detached disks, snapshots, reserved IPs or suspended instances may still
incur charges according to the provider. Delete unneeded resources only after saving what you
need, and revoke a temporary Hugging Face token when the rental is finished.
