# Security Policy

## 1. Supported Versions

PolyPulse maintains security updates for the following releases:

| Version | Supported |
| :--- | :--- |
| `0.1.x` (current master) | :white_check_mark: |
| `< 0.1.0` | :x: |

---

## 2. Reporting a Vulnerability

We take the security of quantitative infrastructure and private API credentials seriously.

If you discover a security vulnerability, please do **NOT** open a public GitHub issue. Instead, disclose it responsibly by contacting the maintainer directly:

- **Security Contact**: `shatwik.mitblr2024@learner.manipal.edu`
- **Response Time**: We acknowledge receipt of vulnerability reports within 48 hours and aim to provide a remediation plan within 5 business days.

Please provide:
- A description of the vulnerability.
- Steps or a minimal proof of concept to reproduce the issue.
- Potential impact on private keys, network traffic, or state consistency.

---

## 3. Security Invariants & Best Practices

### 3.1 Credential Protection
- **Never commit private keys or API keys**: The `.env` file and private key paths (`KALSHI_PRIVATE_KEY_PATH`) must remain strictly local and excluded from git tracking.
- **Strict File Permissions**: On Linux/macOS hosts, restrict private keys:
  ```bash
  chmod 600 /path/to/kalshi-key.pem
  ```

### 3.2 Production Mode Safeguards
- When running with `POLYPULSE_MODE=live`, the manual trade ingestion route (`POST /api/v1/trades`) is automatically disabled with HTTP 403 Forbidden to prevent unauthorized tampering with trade journal data.

### 3.3 Container Security
- Official Docker containers run without elevated host privileges.
- ClickHouse runs inside an isolated internal docker network and only exposes port 8123 where configured.
