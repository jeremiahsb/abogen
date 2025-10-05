# abogen <img width="40px" title="abogen icon" src="https://raw.githubusercontent.com/denizsafak/abogen/refs/heads/main/abogen/assets/icon.ico" align="right" style="padding-left: 10px; padding-top:5px;">

Abogen is a web-first text-to-speech workstation. Drop in an EPUB, PDF, Markdown, or plain text file and Abogen will turn it into high-quality audio with perfectly synced subtitles. The new interface runs entirely inside your browser using Flask + htmx, so it behaves like a modern web app whether you launch it locally or from a container.

## Highlights
- Natural-sounding speech powered by Kokoro-82M with per-job voice, speed, GPU toggle, and subtitle style controls
- Clean dashboard that tracks the status, progress, and logs of every job in real time (thanks to htmx partial updates)
- Automatic chapter detection and subtitle generation with SRT/ASS exports
- Runs well in Docker, ships a REST-style JSON API, and works across macOS, Linux, and Windows

## Quick start
Abogen supports Python 3.10–3.12.

### Install with pip
```bash
python -m venv .venv
source .venv/bin/activate  # On Windows use: .venv\Scripts\activate
pip install abogen
```

### Launch the web app
```bash
abogen
```

Then open http://localhost:8000 and drag in your documents. Jobs run in the background worker and the browser updates automatically.

> **Tip:** Keep the terminal open while the server is running. Use `Ctrl+C` to stop it.

## Container image
A lightweight Dockerfile lives in `abogen/Dockerfile`.

```bash
docker build -t abogen .
mkdir -p ~/abogen-data/uploads ~/abogen-data/outputs
docker run --rm \
  -p 8000:8000 \
  -v ~/abogen-data:/data \
  --name abogen \
  abogen
```

Browse to http://localhost:8000. Uploaded source files are stored in `/data/uploads` and rendered audio/subtitles appear in `/data/outputs`.

### Container environment variables
| Variable | Default | Purpose |
|----------|---------|---------|
| `ABOGEN_HOST` | `0.0.0.0` | Bind address for the Flask server |
| `ABOGEN_PORT` | `8000` | HTTP port |
| `ABOGEN_DEBUG` | `false` | Enable Flask debug mode |
| `ABOGEN_UPLOAD_ROOT` | `/data/uploads` | Directory where uploaded files are stored |
| `ABOGEN_OUTPUT_ROOT` | `/data/outputs` | Directory for generated audio and subtitles |

Set any of these with `-e VAR=value` when starting the container.

### Docker Compose (GPU by default)
The repo includes `docker-compose.yaml`, which targets GPU hosts out of the box. Install the NVIDIA Container Toolkit and run:

```bash
docker compose up -d --build
```

Key build/runtime knobs:

- `TORCH_VERSION` – pin a specific PyTorch release that matches your driver (leave blank for the latest on the configured index).
- `TORCH_INDEX_URL` – swap out the PyTorch download index when targeting a different CUDA build.
- `ABOGEN_DATA` – host path that stores uploads/outputs (defaults to `./data`).

CPU-only deployment: comment out the `deploy.resources.reservations.devices` block (and the optional `runtime: nvidia` line) inside the compose file. Compose will then run without requesting a GPU. If you prefer the classic CLI:

```bash
docker build -f abogen/Dockerfile -t abogen-gpu .
docker run --rm \
  --gpus all \
  -p 8000:8000 \
  -v ~/abogen-data:/data \
  abogen-gpu
```

## GPU acceleration
Abogen detects CUDA automatically. To use an NVIDIA GPU, install the matching PyTorch build before installing Abogen:
```bash
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
pip install abogen
```

On Linux with AMD GPUs, install PyTorch/ROCm nightly wheels:
```bash
pip install --pre torch torchvision torchaudio --index-url https://download.pytorch.org/whl/nightly/rocm6.4
```
Abogen falls back to CPU rendering if no GPU is available.

## Using the web UI
1. Upload a document (drag & drop or use the upload button).
2. Choose voice, language, speed, subtitle style, and output format.
3. Click **Create job**. The job immediately appears in the queue.
4. Watch progress and logs update live. Download audio/subtitle assets when complete.
5. Cancel or delete jobs any time. Download logs for troubleshooting.

Multiple jobs can run sequentially; the worker processes them in order.

## JSON endpoints
Need machine-readable status updates? The dashboard calls a small set of helper endpoints you can reuse:
- `GET /api/jobs/<id>` returns job metadata, progress, and log lines in JSON.
- `GET /partials/jobs` renders the live job list as HTML (htmx uses this for polling).
- `GET /partials/jobs/<id>/logs` renders just the log window.

More automation hooks are planned; contributions are very welcome if you need additional routes.

## Configuration reference
Most behaviour is controlled through the UI, but a few environment variables are helpful for automation:
- `ABOGEN_SECRET_KEY` – provide your own random secret when deploying across multiple replicas.
- `ABOGEN_DEBUG` – set to `true` for verbose Flask error output.

If unset, Abogen picks sensible defaults suitable for local usage.

## Development workflow
```bash
git clone https://github.com/denizsafak/abogen.git
cd abogen
python -m venv .venv
source .venv/bin/activate
pip install -e .
pip install pytest
```

Run the server in development mode:
```bash
export ABOGEN_DEBUG=true
abogen
```

Static files live in `abogen/web/static`, templates in `abogen/web/templates`, and the conversion pipeline in `abogen/web/conversion_runner.py`.

## Tests
```bash
python -m pytest
```

Unit tests cover the queue service, web routes, and conversion pipeline helpers. Contributions that add features should include new tests whenever practical.

## Upgrading from the desktop GUI
The legacy PyQt5 interface is no longer packaged. Existing scripts that call `abogen.main` should switch to the new web entry point (`abogen.web.app:main`). The new experience works headlessly, plays nicely in Docker, and exposes JSON APIs for automation.

## Troubleshooting
- Conversion jobs stay pending → ensure the background worker has write access to the upload/output directories.
- GPU not detected → verify the correct PyTorch wheel is installed (`pip show torch`) and drivers match the container/host.
- Subtitle files missing → check the job configuration; subtitles are optional and can be disabled per job.
- Logs are empty → run with `ABOGEN_DEBUG=true` to get verbose Flask error output in the server console.

If you hit a bug, open an issue describing the input file and the exact log output.

## Contributing
Pull requests are welcome! Please:
- Keep changes focused and well-tested
- Run `python -m pytest`
- Update documentation when behaviour changes

Thanks for helping make Abogen a great open-source audiobook generator.
