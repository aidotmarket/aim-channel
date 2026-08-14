#!/usr/bin/env bash
set -e

CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m'

info() { echo -e "  ${CYAN}▸${NC} $*"; }
pass() { echo -e "  ${GREEN}✔${NC} $*"; }
warn() { echo -e "  ${YELLOW}!${NC} $*"; }
die()  { echo -e "\n  ${RED}✘${NC} $*\n"; exit 1; }

REPO_RAW="https://raw.githubusercontent.com/aidotmarket/aim-data/main"
COMPOSE_URL="${REPO_RAW}/docker-compose.aim-data.yml"
IMAGE="ghcr.io/aidotmarket/aim-data:${AIM_DATA_VERSION:-v1.22.4}"
INSTALL_DIR="${AIM_DATA_INSTALL_DIR:-$HOME/aim-data}"
COMPOSE_FILE="docker-compose.aim-data.yml"

generate_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 16
  else
    date +%s%N | shasum | awk '{print $1}' | cut -c1-32
  fi
}

echo
echo -e "${CYAN}${BOLD}  ⚡ AIM-Data Installer${NC}"
echo

if ! command -v docker >/dev/null 2>&1; then
  die "Docker is not installed. Install Docker Desktop or Docker Engine, then re-run this script.
      https://docs.docker.com/get-docker/"
fi

if ! docker info >/dev/null 2>&1; then
  die "Docker is installed but the daemon is not running. Start Docker Desktop / the service, then re-run."
fi
pass "Docker is ready"

mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"
pass "Install dir: $INSTALL_DIR"

info "Downloading ${COMPOSE_FILE}..."
if command -v curl >/dev/null 2>&1; then
  curl -fsSL "$COMPOSE_URL" -o "$COMPOSE_FILE"
elif command -v wget >/dev/null 2>&1; then
  wget -q "$COMPOSE_URL" -O "$COMPOSE_FILE"
else
  die "Neither curl nor wget is available."
fi
pass "Downloaded compose file"

if [[ ! -f .env ]]; then
  cat > .env <<EOF
# AIM-Data configuration
POSTGRES_PASSWORD=$(generate_secret)
VECTORAIZ_SECRET_KEY=$(generate_secret)
VECTORAIZ_CHANNEL=aim-data
AIM_DATA_PORT=8080
VECTORAIZ_MODE=connected
# Per-install marketplace signing identity. Derives the keystore that signs
# your publish requests to ai.market. Generated once on install — back up this
# value to preserve your seller identity across reinstalls.
AIM_DATA_KEYSTORE_PASSPHRASE=$(generate_secret)
EOF
  pass "Generated .env"
else
  info ".env already exists — keeping it"
fi

# Self-heal: every install MUST carry a non-empty signing passphrase or the
# marketplace publish cannot sign its requests. Repairs .env files written by
# older installers that predate this step.
if ! grep -q '^AIM_DATA_KEYSTORE_PASSPHRASE=.' .env 2>/dev/null; then
  sed -i.bak '/^AIM_DATA_KEYSTORE_PASSPHRASE=$/d' .env 2>/dev/null || true
  rm -f .env.bak
  echo "AIM_DATA_KEYSTORE_PASSPHRASE=$(generate_secret)" >> .env
  pass "Provisioned marketplace signing passphrase"
fi

info "Pulling ${IMAGE}..."
docker pull "$IMAGE" || die "Failed to pull ${IMAGE}"
VERSION=$(docker inspect --format '{{ index .Config.Labels "version" }}' "$IMAGE" 2>/dev/null || true)
VERSION="${VERSION:-unknown}"
pass "Image pulled"

info "Starting AIM-Data..."
docker compose -f "$COMPOSE_FILE" up -d || die "docker compose up failed"
pass "Containers started"
pass "AIM Data ${VERSION} installed"

PORT=$(grep '^AIM_DATA_PORT=' .env | cut -d= -f2)
PORT="${PORT:-8080}"
URL="http://localhost:${PORT}"

echo
echo -e "${GREEN}${BOLD}  ✅ AIM-Data is running${NC}"
echo -e "     URL:   ${CYAN}${URL}${NC}"
echo -e "     Dir:   ${INSTALL_DIR}"
echo -e "     Logs:  docker compose -f ${INSTALL_DIR}/${COMPOSE_FILE} logs -f"
echo
