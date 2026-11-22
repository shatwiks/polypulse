# Change Report

## 2026-11-22 07:10 IST — Pre-LinkedIn Production Security Hardening

### Objective
Harden public repository, API middleware, authentication, CORS, error handling, and CI workflow permissions prior to public LinkedIn sharing.

### Files Modified & Created
- `backend/main.py` — Switched `OPERATOR_KEY` comparison to constant-time `hmac.compare_digest`, enforced fail-closed `403` when `POLYPULSE_MODE=live` and `OPERATOR_KEY` is unset, added HTTP security headers middleware, and switched CORS to explicit `CORS_ORIGINS` env allowlist.
- `.github/workflows/ci.yml` — Added least-privilege `permissions: contents: read`.

### Verification Results
- `python -m py_compile backend/main.py` (PASS), `yaml.safe_load(.github/workflows/ci.yml)` (PASS).
