# AIM Data — Build Guide

## Docker Images

There are TWO Dockerfiles. Using the wrong one is the #1 recurring build error.

| File | Purpose | Port | Includes Frontend? | When to use |
|------|---------|------|-------------------|-------------|
| `Dockerfile.customer` | **Customer deployment** | 80 (nginx) | YES — frontend + nginx + backend | Local customer-image checks and the tag-driven release workflow |
| `Dockerfile` | Railway backend only | 8000 (uvicorn) | NO | Railway auto-deploy only. Never push to GHCR. |

## Local customer-image build

```bash
docker build -f Dockerfile.customer \
  --build-arg VERSION=dev-local \
  -t aim-data:dev-local .

docker image inspect --format '{{ index .Config.Labels "version" }}' aim-data:dev-local
```

The inspect command must print `dev-local`. Use `Dockerfile.customer` when checking the complete customer image; use `Dockerfile` only for Railway backend development.

## Publishing releases

Do not push images, create release tags, or hand-edit embedded version defaults. Follow [docs/RELEASING.md](docs/RELEASING.md): `scripts/release-aim-data.sh` is the single release entry point, and the tag-driven workflow builds and publishes `ghcr.io/aidotmarket/aim-data` only after its required checks.

The release script updates these customer-facing defaults together during stable promotion:

1. `docker-compose.aim-data.yml`
2. `installers/aim-data/install.sh`
3. `installers/aim-data/install.ps1`

## Verification from the customer's perspective

For a published version, set the version explicitly and start the supported compose stack:

```bash
AIM_DATA_VERSION=vX.Y.Z docker compose -f docker-compose.aim-data.yml pull
AIM_DATA_VERSION=vX.Y.Z docker compose -f docker-compose.aim-data.yml up -d
# Wait for the app health check to pass.
curl -s http://localhost:8080/           # Must return HTML (frontend)
curl -s http://localhost:8080/api/health # Must return JSON (API via nginx proxy)
```

If `localhost:8080` returns `ERR_EMPTY_RESPONSE`, confirm the image was built from `Dockerfile.customer` and inspect `docker compose -f docker-compose.aim-data.yml logs app`.

## Docker binary on this machine

OrbStack: `/Users/max/.orbstack/bin/docker`
