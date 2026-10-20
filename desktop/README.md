# Prisma Desktop

Prisma's primary Windows application is a Tauri v2 desktop window. It
health-gates the Python service and does not require an external browser.
The local single-user sidecar opens directly. Prisma does not ship a login
screen or account-management interface.

## Development

Prerequisites: Rust, Cargo, Node.js, npm, and the Python dependencies from the
repository root.

```powershell
cd desktop
npm install
npx tauri dev
```

The desktop shell launches a PyInstaller-packaged backend when present and
falls back to `python -m uvicorn` during development.

Build the bundled backend:

```powershell
pwsh scripts/build-server.ps1
```

Build the Windows MSI:

```powershell
cd desktop
npm ci
npx tauri build
```

The MSI is written under `src-tauri/target/release/bundle/msi/`.

## Local desktop mode

The desktop process starts a loopback-only backend and injects:

```dotenv
AUTH_ENABLED=false
LOCALHOST_BYPASS=false
PRISMA_DESKTOP_MODE=true
```

Authentication cannot be re-enabled with an environment variable.

## Use the DGX Spark without a browser

Keep Prisma private on the Spark:

```dotenv
AUTH_ENABLED=false
LOCALHOST_BYPASS=false
APP_BIND=127.0.0.1
```

Configure the Windows desktop client once:

```powershell
powershell -ExecutionPolicy Bypass -File .\configure-spark-client.ps1
```

Before opening **Prisma Desktop** from the Start menu, open this private tunnel
and leave its PowerShell window running:

```powershell
ssh -L "[::1]:7000:127.0.0.1:7000" sparky@spark-132a
```

The script saves `%APPDATA%\Prism\client.json` as:

```json
{"url":"http://[::1]:7000","spawn_sidecar":false}
```

The explicit `spawn_sidecar: false` prevents a second local backend from
starting when the loopback address is actually an SSH tunnel to the Spark.
Using IPv6 loopback also prevents an old IPv4 development server on port 7000
from intercepting the desktop connection.

Prisma Desktop is not a shared multi-user server. Keep the raw application
port on loopback. Use the SSH tunnel above when the backend runs on a Spark.

## Environment overrides

- `PRISM_URL`: backend URL.
- `PRISM_SPAWN_SIDECAR`: explicitly enable or disable the local backend.
- `PRISM_TOKEN`: bridge secret; generated in memory when unset.
- `PRISM_SKILLS_DIR`: optional shared skills directory.
- `PRISM_REPO_ROOT`: optional repository-root override.

## Tests

```powershell
cd src-tauri
cargo test --lib
```
