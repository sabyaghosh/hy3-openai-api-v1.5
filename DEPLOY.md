# Deployment Guide

This repo's `Dockerfile` is portable — you can deploy the Hy3 OpenAI-compatible API on any container-friendly host. Below are setup guides for the most common alternatives to Render.

All platforms end up exposing the same surface:

```
GET  /                  — service info
GET  /health            — liveness probe
GET  /stats             — runtime counters
GET  /v1/models         — OpenAI models list
POST /v1/chat/completions — OpenAI chat completion (streaming + non-streaming)
```

Tune concurrency via env vars `MAX_CONCURRENT` (default `10`) and `QUEUE_TIMEOUT` (default `5` seconds).

---

## 1. Fly.io (recommended — best free tier, no sleep)

Fly gives you 3 shared-CPU VMs (256MB RAM each) for free, with no idle sleep and configurable request timeouts up to several minutes.

### Steps

```bash
# Install flyctl
curl -L https://fly.io/install.sh | sh

# Clone & cd
git clone https://github.com/sabyaghosh/hy3-openai-api.git
cd hy3-openai-api

# Launch (creates app + region)
fly launch --no-deploy
# Answer: yes to copy existing Dockerfile, pick a region close to your users

# Set env vars
fly secrets set MAX_CONCURRENT=10 QUEUE_TIMEOUT=5

# Extend request timeout to 5 minutes (avoids the Render-style 100s cutoff)
fly deploy
fly scale memory 512          # bump from 256MB if you see OOM
```

### `fly.toml` (auto-generated, edit the `[http_service]` block)

```toml
[http_service]
  internal_port = 8000
  force_https = true
  auto_stop_machines = false   # keep warm — no cold starts
  auto_start_machines = true
  min_machines_running = 1     # never sleep

  [http_service.concurrency]
    type = "requests"
    hard_limit = 50
    soft_limit = 25
```

URL: `https://<app-name>.fly.dev/v1`

---

## 2. Google Cloud Run (generous free tier, scales to zero)

Cloud Run's free tier includes 2M requests/month + 360k vCPU-seconds. Request timeout can be set up to **60 minutes**.

### Steps

```bash
# Install gcloud CLI: https://cloud.google.com/sdk/docs/install
gcloud auth login
gcloud config set project YOUR_PROJECT_ID

# Enable Cloud Run + Artifact Registry
gcloud services enable run.googleapis.com artifactregistry.googleapis.com

# Build & push image
gcloud artifacts repositories create hy3 --repository-format=docker --location=us-central1
gcloud builds submit --tag us-central1-docker.pkg.dev/YOUR_PROJECT_ID/hy3/api

# Deploy with 5-minute timeout, 10 max concurrent requests per instance
gcloud run deploy hy3-openai-api \
  --image us-central1-docker.pkg.dev/YOUR_PROJECT_ID/hy3/api \
  --region us-central1 \
  --platform managed \
  --port 8000 \
  --timeout 300 \
  --concurrency 10 \
  --cpu 1 \
  --memory 512Mi \
  --min-instances 0 \
  --max-instances 10 \
  --allow-unauthenticated \
  --set-env-vars MAX_CONCURRENT=10,QUEUE_TIMEOUT=5
```

URL: `https://hy3-openai-api-<hash>-uc.a.run.app/v1`

**Note:** `--concurrency 10` is Cloud Run's per-instance cap. Set `MAX_CONCURRENT=10` in env to match — they should agree so the semaphore fails fast inside the app before Cloud Run's own throttling kicks in.

---

## 3. Koyeb (free, no sleep, Docker-native)

Koyeb's free tier gives you 1 service with 512MB RAM, no idle sleep, and no fixed request timeout.

### Steps

1. Sign up at https://koyeb.com
2. **Create Service → GitHub → select repo `sabyaghosh/hy3-openai-api`**
3. Builder: **Dockerfile**
4. Path: `/` (root)
5. Port: `8000`
6. Env vars:
   - `MAX_CONCURRENT=10`
   - `QUEUE_TIMEOUT=5`
   - `PYTHONUNBUFFERED=1`
7. **Deploy**

URL: `https://<service-name>-<org>.koyeb.app/v1`

CLI alternative:

```bash
curl -fsSL https://www.koyeb.com/install.sh | sh
koyeb login
koyeb service create hy3-openai-api \
  --github sabyaghosh/hy3-openai-api \
  --branch main \
  --dockerfile Dockerfile \
  --port 8000 \
  --env MAX_CONCURRENT=10 \
  --env QUEUE_TIMEOUT=5 \
  --routes /:8000
```

---

## 4. Oracle Cloud Always Free (best free compute, self-managed)

Oracle's free tier includes a beefy **VM.Standard.A1.Flex** ARM instance: 4 OCPUs + 24GB RAM. No idle sleep. You manage the OS.

### Steps

```bash
# After creating an Always Free A1 instance (Ubuntu 22.04):
ssh ubuntu@<your-instance-ip>

# Install Docker
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
newgrp docker

# Clone & run
git clone https://github.com/sabyaghosh/hy3-openai-api.git
cd hy3-openai-api
docker build -t hy3-openai-api .

# Run on port 80
docker run -d --name hy3 \
  --restart unless-stopped \
  -p 80:8000 \
  -e MAX_CONCURRENT=20 \
  -e QUEUE_TIMEOUT=5 \
  hy3-openai-api

# Open ingress: OCI Console → VCN → Security List → add TCP 80 from 0.0.0.0/0
```

URL: `http://<instance-public-ip>/v1`

For HTTPS, front it with Caddy or nginx-proxy + Let's Encrypt.

---

## 5. Railway (closest to Render UX, $5/mo after trial)

Railway has no idle sleep on paid plans and no fixed request timeout. $5/mo gets you 500 hours + 1GB RAM shared across services.

### Steps

1. Go to https://railway.app/new
2. **Deploy from GitHub repo → select `sabyaghosh/hy3-openai-api`**
3. Railway auto-detects `Dockerfile`
4. **Settings → Networking → Generate Domain**
5. **Variables tab → add:**
   - `MAX_CONCURRENT=10`
   - `QUEUE_TIMEOUT=5`
   - `PORT=8000` (Railway sets this automatically, but explicit doesn't hurt)

URL: `https://<project>.up.railway.app/v1`

---

## 6. Hugging Face Spaces (free, Docker, same upstream as Hy3)

HF Spaces runs Docker for free (2 vCPU / 16GB RAM). It sleeps after 48h idle, but since Hy3 itself is hosted on HF, network calls to the upstream are intra-HF and very fast.

### Steps

1. Go to https://huggingface.co/new-space
2. **SDK: Docker**
3. **Files → Add file → upload the repo contents** (or `git clone https://huggingface.co/spaces/<your-user>/hy3-api` and push)
4. **Settings → Repository secrets → add:**
   - `MAX_CONCURRENT=10`
   - `QUEUE_TIMEOUT=5`
5. The `Dockerfile` exposes port 8000 by default; HF Spaces requires you to expose on `7860`. Edit the Dockerfile's `EXPOSE`/`CMD` or set `PORT=7860` in Space variables.

URL: `https://<your-user>-hy3-api.hf.space/v1`

---

## Platform comparison

| Platform | Free? | Sleeps? | Timeout | Concurrency cap | Best for |
|---|---|---|---|---|---|
| **Fly.io** | Yes (3 VMs) | No | 5 min (configurable) | 25/hard 50/soft | Production free tier |
| **Cloud Run** | Yes (2M req/mo) | Yes (scales to 0) | 60 min | configurable | Bursty workloads |
| **Koyeb** | Yes (1 service) | No | None | None | Simple always-on |
| **Oracle Cloud** | Yes (4 ARM cores) | No | None | None | High-throughput self-host |
| **Railway** | Trial ($5 credit) | No (paid) | None | None | Render migration |
| **Hugging Face Spaces** | Yes | Yes (48h) | None | None | Co-located with Hy3 upstream |
| **Render (current)** | Yes | Yes (15 min) | ~100s | None | Quick demos |

## Recommendation

For your use case (OpenAI-compatible proxy with tool calls + occasional bursts):

- **Quick win → Fly.io**: same Dockerfile, just `fly launch && fly deploy`. No sleep, configurable timeout, decent free tier.
- **Best free compute → Oracle Cloud A1**: 4 ARM cores + 24GB RAM is absurdly generous for free. You'll need to manage the OS.
- **If you don't mind cold starts → Cloud Run**: 60-min timeout means you'll never hit a wall, and the 2M req/mo free tier is plenty.
- **Stay on Render but tune the semaphore**: with `MAX_CONCURRENT=5` and `QUEUE_TIMEOUT=2`, you trade throughput for stability — no more 45s timeouts, just clean 503s when overloaded.
